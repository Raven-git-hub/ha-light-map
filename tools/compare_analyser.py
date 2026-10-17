"""Compare analyser: run the analyser twice over identical input and diff the almanacs.

    python tools/compare_analyser.py --legacy-csv path/to/heartbeat_YYYY-MM-DD.csv
    python tools/compare_analyser.py --config /data/config.json --tz Europe/London [--today ...]

Used to prove that a change to the learning maths does what was intended and nothing else:
the same readings go through the analyser under two sets of `AnalyserFlags` (by default the
n8n pipeline's, `analyser.LEGACY`, against the current defaults), and every learned value is
printed side by side. Change a flag's default, or add a flag for a new change, and run it.

Two inputs:

- `--legacy-csv`: the predecessor's historical wide heartbeat CSV (`timestamp`,
  `ambient_lux`, `motion_detected`, `any_light_on`, `group_1_brightness` ...), which predates
  its section column. It is loaded into a scratch store, each group as one bulb in a unit of
  its own (the shape the predecessor learned per group), and sections are assigned from
  assumed boundaries: fine for comparing two analysers on identical input, not a statement
  about any real installation. A group's "on" is its brightness above 0, and Light Map
  works out "any light on" from the bulbs rather than taking the CSV's column.
- `--config`: a committed config and its store (`system.data_dir`): every adaptive room,
  pooled into its learning units as the nightly pass would. It needs HA's time zone
  (`--tz`): it files each reading under its day, and a wrong one would misfile the readings
  near midnight without a word.

Ported from ha-adaptive-light's `tools/compare_analyser.py`. Read-only: it never contacts
Home Assistant, and never opens a live store: it reads a copy of the config's CSV archive (the
source of truth) into a scratch store, so a running Light Map's query layer is never touched.
"""

import argparse
import csv
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    # Run as a script (`python tools/compare_analyser.py`): put the repo root on the path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import config as config_mod  # noqa: E402
from app.core.config import SECTIONS  # noqa: E402
from app.core.store import ARCHIVE_DIR, Store  # noqa: E402
from app.modules.adaptive import analyser  # noqa: E402
from app.modules.adaptive.analyser import AnalyserFlags, LearningConfig  # noqa: E402
from app.modules.adaptive.generate import adaptive_rooms, learning_units  # noqa: E402

# The predecessor's assumed section starts for its pre-section CSV, and its four groups.
BOUNDS = (
    ("sunrise", "05:30"),
    ("day", "10:00"),
    ("afternoon", "16:00"),
    ("sunset", "19:00"),
    ("night", "20:30"),
    ("sleep", "22:00"),
)
GROUPS = ("tv_light", "play_area", "dining_area", "bar_lights")
ROOM = "main_room"


def section_for(hhmm: str) -> str:
    current = "sleep"
    for name, start in BOUNDS:
        if hhmm >= start:
            current = name
    return current if hhmm >= "05:30" else "sleep"


def load_legacy_csv(
    path: Path, store: Store, *, tz: ZoneInfo, room_id: str = ROOM, groups: Sequence[str] = GROUPS
) -> int:
    """Load the predecessor's wide heartbeat CSV into `store`, one bulb per group; return
    how many heartbeats it held. A naive timestamp is taken as local time in `tz`."""
    count = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            moment = datetime.fromisoformat(row["timestamp"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tz)
            local = moment.astimezone(tz)
            readings = []
            for i, group in enumerate(groups, start=1):
                brightness = int(row[f"group_{i}_brightness"])
                readings.append(
                    {"bulb_id": group, "is_on": brightness > 0, "brightness": brightness}
                )
            store.append_heartbeat(
                {
                    "ts": moment,
                    "room_id": room_id,
                    "section": section_for(local.strftime("%H:%M")),
                    "lux": float(row["ambient_lux"]),
                    "lux_sensors": 1,
                    "occupancy": row["motion_detected"] == "True",
                },
                readings,
            )
            count += 1
    return count


def compare(
    store: Store,
    rooms: Iterable[tuple[str, Mapping[str, Sequence[str]], bool]],
    *,
    today: date,
    tz: ZoneInfo,
    learning: Mapping[str, Any] | None = None,
    a: AnalyserFlags = analyser.LEGACY,
    b: AnalyserFlags | None = None,
) -> list[dict[str, Any]]:
    """Each room's almanac under flags `a` and `b` (default: the current ones), as rows of
    (room, section, unit or None for the room's lux target, a's value, b's value, b's
    on-fraction). `rooms` is (room id, units: unit id -> bulb ids, presence configured)."""
    rows: list[dict[str, Any]] = []
    for room_id, units, presence in rooms:
        results = [
            analyser.analyse_room(
                store,
                room_id,
                units,
                LearningConfig.from_config(learning, flags),
                today,
                tz=tz,
                presence_configured=presence,
            )
            for flags in (a, b or AnalyserFlags())
        ]
        first, second = results
        for section in SECTIONS:
            old, new = first.get(section), second.get(section)
            if old is None and new is None:
                rows.append(
                    {
                        "room": room_id,
                        "section": section,
                        "unit": None,
                        "a": None,
                        "b": None,
                        "empty": True,
                    }
                )
                continue
            rows.append(
                {
                    "room": room_id,
                    "section": section,
                    "unit": None,
                    "a": (old or {}).get("lux_target"),
                    "b": (new or {}).get("lux_target"),
                }
            )
            for unit in sorted(units):
                rows.append(
                    {
                        "room": room_id,
                        "section": section,
                        "unit": unit,
                        "a": ((old or {}).get("units") or {}).get(unit),
                        "b": ((new or {}).get("units") or {}).get(unit),
                        "on_fraction": ((new or {}).get("on_fraction") or {}).get(unit),
                    }
                )
    return rows


def print_rows(rows: Sequence[Mapping[str, Any]], labels: tuple[str, str]) -> None:
    print(f"{'room':<14} {'section':<10} {'unit':<14} {labels[0]:>8} {labels[1]:>8} {'on_frac':>8}")
    print("-" * 68)
    for row in rows:
        if row.get("empty"):
            print(f"{row['room']:<14} {row['section']:<10} (no data)")
            continue
        unit = row["unit"] or "LUX TARGET"
        on = row.get("on_fraction")
        print(
            f"{row['room']:<14} {row['section']:<10} {unit:<14} "
            f"{_cell(row['a']):>8} {_cell(row['b']):>8} {'' if on is None else format(on, '.0%'):>8}"
        )


def _cell(value: Any) -> str:
    return "-" if value is None else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--legacy-csv", type=Path, help="the predecessor's wide heartbeat CSV")
    source.add_argument("--config", type=Path, help="a committed config, with its store")
    parser.add_argument("--today", type=date.fromisoformat, help="the pass's date (default: today)")
    parser.add_argument(
        "--tz",
        help="HA's time zone, which files each reading under its day (required with --config; "
        "default for the predecessor's CSV: Europe/London, its house's)",
    )
    args = parser.parse_args(argv)
    if args.config and not args.tz:
        parser.error("--config needs --tz: HA's time zone files each reading under its day")
    tz = ZoneInfo(args.tz or "Europe/London")
    today = args.today or datetime.now(tz).date()
    with tempfile.TemporaryDirectory() as scratch:
        store = Store(Path(scratch))
        if args.legacy_csv:
            store.open()
            try:
                load_legacy_csv(args.legacy_csv, store, tz=tz)
                rows = compare(store, [(ROOM, {g: [g] for g in GROUPS}, True)], today=today, tz=tz)
            finally:
                store.close()
        else:
            doc = config_mod.load(args.config)
            archive = config_mod.data_dir(doc, args.config) / ARCHIVE_DIR
            if archive.is_dir():
                shutil.copytree(archive, Path(scratch) / ARCHIVE_DIR)
            store.open()  # rebuilt from the copy
            try:
                rooms = [
                    (
                        room["id"],
                        {u["id"]: [b["id"] for b in u["bulbs"]] for u in learning_units(doc, room)},
                        bool(room["adaptive_config"].get("presence_sensors")),
                    )
                    for room in adaptive_rooms(doc)
                ]
                rows = compare(store, rooms, today=today, tz=tz, learning=doc.get("learning"))
            finally:
                store.close()
    print_rows(rows, ("legacy", "current"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
