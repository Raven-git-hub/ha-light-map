"""Sweep: a year of section-boundary computations, checking for unexpected collapses.

Drives `app.modules.adaptive.scheduler` across every day of a year for each schedule profile
and reports the days where sections collapse unexpectedly.

    python tools/sweep.py [--config /data/config.json] [--profile ID] [--year 2026]

Without `--config` (or `LM_CONFIG_PATH`) it sweeps the standard schedule. For each profile
it computes every day of the year at five latitudes, from the equator to the Arctic (the
failure modes are seasonal and latitudinal: a rule that misbehaves in a Nordic December
looks perfect in a London July), and lists each section's collapses: how many days, their
first and last date, and why. A collapse with a reason (a sun event that doesn't happen, an
order or gap conflict the profile's priorities settle) is the scheduler doing its job, and
the list is there to be read. An *unexpected* one is a day the scheduler got wrong:

- a section that neither ran nor collapsed with a reason, or that is missing or doubled;
- kept sections out of chronological or canonical order;
- two kept sections closer than the profile's `min_section_minutes`;
- a kept section starting outside its day (it would never be in force once the next day's
  first boundary passed it: the predecessor's clamp could do that);
- a kept section's `ends` that isn't the next one's start.

Any of those makes the exit status 1. Ported from ha-adaptive-light's `tools/sweep.py`
(which printed the collapse table), with the invariant check added. Read-only: it never
contacts Home Assistant or writes anything.
"""

import argparse
import os
import sys
from itertools import pairwise
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    # Run as a script (`python tools/sweep.py`): put the repo root on the path for `app`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import config as config_mod  # noqa: E402
from app.modules.adaptive.scheduler import (  # noqa: E402
    CANONICAL_ORDER,
    AstralSunProvider,
    Boundary,
    SunProvider,
    compute_day,
)

# (label, latitude, longitude, time zone): the predecessor's five sites.
SITES = (
    ("Equatorial (1.35N)", 1.35, 103.82, "Asia/Singapore"),
    ("Perth (31.95S)", -31.95, 115.86, "Australia/Perth"),
    ("London (51.51N)", 51.51, -0.13, "Europe/London"),
    ("Helsinki (60.17N)", 60.17, 24.94, "Europe/Helsinki"),
    ("Tromso (69.65N)", 69.65, 18.96, "Europe/Oslo"),
)


@dataclass
class Collapses:
    """One section's collapses over the sweep."""

    days: list[date] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class SweepResult:
    """A profile swept over a year at one site."""

    site: str
    days: int = 0
    collapses: dict[str, Collapses] = field(default_factory=dict)
    unexpected: list[str] = field(default_factory=list)


def check_day(
    boundaries: Sequence[Boundary], profile: Mapping[str, Any], day: date | None = None
) -> list[str]:
    """What is wrong with one day's boundaries (nothing, for a day the scheduler got
    right): see the module docstring for what counts. With `day`, a kept boundary must also
    start on it."""
    problems = []
    sections = sorted(b.section for b in boundaries)
    if sections != sorted(CANONICAL_ORDER):
        problems.append(f"sections {sections}, not each of the six once")
    for b in boundaries:
        if b.outcome not in ("ran", "collapsed"):
            problems.append(f"{b.section}: outcome {b.outcome!r}")
        elif not b.ran and not b.reason:
            problems.append(f"{b.section} collapsed with no reason")
        elif b.ran and b.planned is None:
            problems.append(f"{b.section} ran with no start")
    kept = [b for b in boundaries if b.ran and b.planned is not None]
    gap = timedelta(minutes=profile.get("min_section_minutes", 30))
    for before, after in pairwise(kept):
        if after.planned <= before.planned:
            problems.append(f"{after.section} starts before {before.section}")
        if CANONICAL_ORDER[after.section] <= CANONICAL_ORDER[before.section]:
            problems.append(f"{after.section} runs after {before.section}, out of canonical order")
        if after.planned - before.planned < gap:
            problems.append(f"{before.section} lasts less than {gap}")
    for b in kept:
        if day is not None and b.planned.date() != day:
            problems.append(f"{b.section} starts on {b.planned.date()}, outside its day")
    for i, b in enumerate(kept):
        expected = kept[i + 1].planned if i + 1 < len(kept) else None
        if b.ends != expected:
            problems.append(f"{b.section} ends at {b.ends}, not at the next start")
    return problems


def sweep(
    profile: Mapping[str, Any], provider: SunProvider, year: int, label: str = ""
) -> SweepResult:
    """Every day of `year` for a profile, with the provider's sun (and time zone)."""
    tz = getattr(provider, "tzinfo", None)
    result = SweepResult(label)
    day = date(year, 1, 1)
    while day.year == year:
        boundaries = compute_day(profile, provider, day, tz)
        result.days += 1
        for b in boundaries:
            if not b.ran:
                found = result.collapses.setdefault(b.section, Collapses())
                found.days.append(day)
                reason = _kind(b.reason)
                found.reasons[reason] = found.reasons.get(reason, 0) + 1
        result.unexpected += [
            f"{day}: {problem}" for problem in check_day(boundaries, profile, day)
        ]
        day += timedelta(days=1)
    return result


def _kind(reason: str | None) -> str:
    """A collapse's reason without the date, so a season's worth reads as one line."""
    if not reason:
        return "no reason"
    return reason.split(" on ", 1)[0]


def profiles(config_path: Path | None, only: str | None) -> list[tuple[str, dict[str, Any]]]:
    """The profiles to sweep: the config's (validated), or the standard schedule."""
    if config_path is None:
        found = [("standard", {"id": "standard", **config_mod.standard_schedule()})]
    else:
        try:
            doc = config_mod.load(config_path)
        except config_mod.ConfigError as exc:
            raise SystemExit(f"{config_path} doesn't load: {exc}") from None
        found = [(p["id"], p) for p in doc["schedule_profiles"]]
    if only is not None:
        found = [(pid, p) for pid, p in found if pid == only]
        if not found:
            raise SystemExit(f"no schedule profile '{only}'")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep and report collapses; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, help="a committed config (default: LM_CONFIG_PATH)")
    parser.add_argument("--profile", help="sweep only this profile")
    parser.add_argument("--year", type=int, default=date.today().year)
    args = parser.parse_args(argv)
    path = args.config or (
        Path(os.environ["LM_CONFIG_PATH"]) if os.environ.get("LM_CONFIG_PATH") else None
    )
    unexpected = 0
    for profile_id, profile in profiles(path, args.profile):
        print(
            f"\nProfile '{profile_id}' ({profile.get('collision_policy', 'collapse')}), {args.year}"
        )
        print("-" * 78)
        for label, latitude, longitude, zone in SITES:
            provider = AstralSunProvider(latitude, longitude, ZoneInfo(zone))
            result = sweep(profile, provider, args.year, label)
            print(f"{label:<20} {_summary(result)}")
            for line in result.unexpected[:10]:
                print(f"{'':<20} UNEXPECTED {line}")
            if len(result.unexpected) > 10:
                print(f"{'':<20} ... and {len(result.unexpected) - 10} more")
            unexpected += len(result.unexpected)
    print(f"\n{unexpected} unexpected collapse(s)." if unexpected else "\nNo unexpected collapses.")
    return 1 if unexpected else 0


def _summary(result: SweepResult) -> str:
    if not result.collapses:
        return "no collapses"
    parts = []
    for section, found in sorted(result.collapses.items(), key=lambda item: -len(item[1].days)):
        span = f"{found.days[0]:%d %b}-{found.days[-1]:%d %b}"
        why = "; ".join(f"{reason} x{n}" for reason, n in sorted(found.reasons.items()))
        parts.append(f"{section} x{len(found.days)} ({span}: {why})")
    return "\n" + "\n".join(f"{'':<20} {part}" for part in parts)


if __name__ == "__main__":
    raise SystemExit(main())
