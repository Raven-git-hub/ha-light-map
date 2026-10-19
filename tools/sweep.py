"""Sweep: a year of section-boundary computations, checking for unexpected collapses.

Drives `app.modules.adaptive.scheduler` across every day of a year for each schedule profile
and reports the days where sections collapse unexpectedly.

    python tools/sweep.py [--config /data/config.json] [--profile ID] [--year 2026]

Without `--config` (or `LM_CONFIG_PATH`) it sweeps the standard schedule. For each profile
it computes every day of the year at eleven sites: five latitudes, from the equator to the
Arctic (the failure modes are seasonal and latitudinal: a rule that misbehaves in a Nordic
December looks perfect in a London July), and six time zones chosen for their clock changes
(P6a): New York's at 02:00, Sydney's in the southern spring, Santiago's and Beirut's at
midnight, Nuuk's at the very end of the day, and Lord Howe's half hour. It lists each
section's collapses: how many days, their first and last date, and why. A collapse with a
reason (a sun event that doesn't happen, an order or gap conflict the profile's priorities
settle) is the scheduler doing its job, and the list is there to be read. An *unexpected*
one is a day the scheduler got wrong:

- a section that neither ran nor collapsed with a reason, or that is missing or doubled;
- kept sections out of chronological or canonical order;
- two kept sections closer than the profile's `min_section_minutes`;
- a kept section starting outside its day (it would never be in force once the next day's
  first boundary passed it: the predecessor's clamp could do that), or at a time the clocks
  skip that day;
- a kept section's `ends` that isn't the next one's start;
- a section reported as run that the runtime's own choice (`active_section_at`) doesn't put
  in force from its start until its end, a non-empty stretch of its day (`check_in_force`):
  run on paper, never in force, as the predecessor's wall-clock reading of a spring-forward
  day had it.

Times are ordered and measured as instants, as the runtime takes them. Then, whatever the
profiles, it checks the scheduler itself across every clock change of the year at those
sites: probe profiles whose times step through the night, so that some land in each hour
the clocks skip or repeat, swept over each clock-change day and its neighbours
(`clock_change_probes`, `sweep_days`) against the same invariants.

Any of those makes the exit status 1. Ported from ha-adaptive-light's `tools/sweep.py`
(which printed the collapse table), with the invariant checks added. Read-only: it never
contacts Home Assistant or writes anything.
"""

import argparse
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from datetime import tzinfo as TimeZone
from itertools import pairwise
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
    SunUnavailable,
    active_section_at,
    compute_day,
)

# (label, latitude, longitude, time zone): the predecessor's five sites, then six chosen for
# their clock changes (P6a): New York's at 02:00 local; Sydney's in the southern spring;
# Santiago's (southern) and Beirut's at midnight, so the day itself starts in the gap or
# ends in the repeated hour; Nuuk's at 23:00, so the clocks skip the day's last hour; and
# Lord Howe's, half an hour.
SITES = (
    ("Equatorial (1.35N)", 1.35, 103.82, "Asia/Singapore"),
    ("Perth (31.95S)", -31.95, 115.86, "Australia/Perth"),
    ("London (51.51N)", 51.51, -0.13, "Europe/London"),
    ("Helsinki (60.17N)", 60.17, 24.94, "Europe/Helsinki"),
    ("Tromso (69.65N)", 69.65, 18.96, "Europe/Oslo"),
    ("New York (40.71N)", 40.71, -74.01, "America/New_York"),
    ("Sydney (33.87S)", -33.87, 151.21, "Australia/Sydney"),
    ("Santiago (33.45S)", -33.45, -70.67, "America/Santiago"),
    ("Beirut (33.89N)", 33.89, 35.50, "Asia/Beirut"),
    ("Nuuk (64.18N)", 64.18, -51.72, "America/Nuuk"),
    ("Lord Howe (31.55S)", -31.55, 159.08, "Australia/Lord_Howe"),
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


# -- Real time -------------------------------------------------------------------------
#
# The checks take times as instants, as the runtime does, with helpers of their own rather
# than the scheduler's: a checker that shared the scheduler's reading of a clock change
# would agree with it when it is wrong.


def _instant(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else moment


def _happens(moment: datetime) -> bool:
    """Whether a time's wall clock is ever read that day: not one the clocks skip."""
    if moment.tzinfo is None:
        return True
    back = moment.astimezone(timezone.utc).astimezone(moment.tzinfo)
    return back.replace(tzinfo=None) == moment.replace(tzinfo=None)


def _local(instant: datetime, zone: TimeZone | None) -> datetime:
    return instant.astimezone(zone) if zone is not None else instant


def _day_end(day: date, zone: TimeZone | None) -> datetime:
    """The instant the next day starts: its 00:00, or where the clocks skip midnight, the
    moment they jump (read before the change, a skipped 00:00 is exactly that moment)."""
    return _instant(datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone))


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
        if _instant(after.planned) <= _instant(before.planned):
            problems.append(f"{after.section} starts before {before.section}")
        if CANONICAL_ORDER[after.section] <= CANONICAL_ORDER[before.section]:
            problems.append(f"{after.section} runs after {before.section}, out of canonical order")
        if _instant(after.planned) - _instant(before.planned) < gap:
            problems.append(f"{before.section} lasts less than {gap}")
    for b in kept:
        if day is not None and b.planned.date() != day:
            problems.append(f"{b.section} starts on {b.planned.date()}, outside its day")
        if not _happens(b.planned):
            problems.append(
                f"{b.section} starts at {b.planned:%H:%M}, a time the clocks skip on "
                f"{b.planned.date()}"
            )
    for i, b in enumerate(kept):
        expected = kept[i + 1].planned if i + 1 < len(kept) else None
        if b.ends != expected:
            problems.append(f"{b.section} ends at {b.ends}, not at the next start")
    return problems


def _clock_change(start: datetime, end: datetime, zone: TimeZone | None) -> datetime | None:
    """The first second after `start`, up to `end`, at which the zone's clocks read
    another offset, or None if they don't change between the two."""
    if zone is None:
        return None
    offset = start.astimezone(zone).utcoffset()
    if end.astimezone(zone).utcoffset() == offset:
        return None
    before, after = int(start.timestamp()), int(end.timestamp()) + 1
    while after - before > 1:
        middle = (before + after) // 2
        if datetime.fromtimestamp(middle, zone).utcoffset() == offset:
            before = middle
        else:
            after = middle
    return datetime.fromtimestamp(after, timezone.utc)


def check_in_force(
    boundaries: Sequence[Boundary],
    profile: Mapping[str, Any],
    provider: SunProvider,
    zone: TimeZone | None = None,
    *,
    days: Callable[[date], Sequence[Boundary]] | None = None,
) -> list[str]:
    """What the runtime would disagree with in one day's report (nothing, for a day the
    scheduler got right): every section reported as run must be the one
    `active_section_at` (the runtime's own choice of the section in force) gives at its
    start, just after the clocks change if they do while it is in force (a repeated hour
    could take it back), and still one second before its end (the next kept start, or for
    the day's last, the next day's start). The runtime's choice only moves on as kept
    starts pass, so that puts it in force over that whole, non-empty stretch of its day.
    Where they disagree, the Now tab shows a section that never comes into force, or one
    the runtime would leave and fire again. `days` gives the boundaries of a day already
    computed (`active_section_at`'s own)."""
    problems = []
    kept = [b for b in boundaries if b.ran and b.planned is not None]
    for i, b in enumerate(kept):
        start = _instant(b.planned)
        end = (
            _instant(kept[i + 1].planned) if i + 1 < len(kept) else _day_end(b.planned.date(), zone)
        )
        if end <= start:
            problems.append(
                f"{b.section} is never in force: the next section starts no later than it does"
            )
            continue
        moments = [("starts", start)]
        change = _clock_change(start, end - timedelta(seconds=1), zone)
        if change is not None:
            moments.append(("is in force and the clocks change", change))
        moments.append(("ends", end - timedelta(seconds=1)))
        for label, moment in moments:
            try:
                section, since = active_section_at(
                    profile, provider, _local(moment, zone), zone, days=days
                )
            except SunUnavailable:
                section, since = None, None
            if section != b.section or since is None or _instant(since) != start:
                found = f"{section} (from {since:%H:%M})" if since is not None else "nothing"
                problems.append(
                    f"{b.section} is reported in force from {b.planned:%H:%M}, but just as it "
                    f"{label} the runtime has {found} in force"
                )
                break
    return problems


class _Days:
    """A profile's days at a place, each computed once (`compute_day`): the in-force check
    asks `active_section_at` about every section of every day swept, and it looks at the
    same days again."""

    def __init__(self, profile: Mapping[str, Any], provider: SunProvider) -> None:
        self.profile, self.provider = profile, provider
        self.tzinfo = getattr(provider, "tzinfo", None)
        self.seen: dict[date, list[Boundary]] = {}

    def __call__(self, day: date) -> list[Boundary]:
        if day not in self.seen:
            self.seen[day] = compute_day(self.profile, self.provider, day, self.tzinfo)
        return self.seen[day]


def sweep(
    profile: Mapping[str, Any], provider: SunProvider, year: int, label: str = ""
) -> SweepResult:
    """Every day of `year` for a profile, with the provider's sun (and time zone)."""
    day, days = date(year, 1, 1), []
    while day.year == year:
        days.append(day)
        day += timedelta(days=1)
    return sweep_days(profile, provider, days, label)


def sweep_days(
    profile: Mapping[str, Any], provider: SunProvider, days: Iterable[date], label: str = ""
) -> SweepResult:
    """The given days for a profile, with the provider's sun (and time zone), each checked
    against every invariant (`check_day`, `check_in_force`)."""
    computed = _Days(profile, provider)
    result = SweepResult(label)
    for day in days:
        boundaries = computed(day)
        result.days += 1
        for b in boundaries:
            if not b.ran:
                found = result.collapses.setdefault(b.section, Collapses())
                found.days.append(day)
                reason = _kind(b.reason)
                found.reasons[reason] = found.reasons.get(reason, 0) + 1
        problems = check_day(boundaries, profile, day)
        problems += check_in_force(boundaries, profile, provider, computed.tzinfo, days=computed)
        result.unexpected += [f"{day}: {problem}" for problem in problems]
    return result


# -- Clock changes ---------------------------------------------------------------------


def clock_changes(zone: TimeZone, year: int) -> list[date]:
    """The days of `year` on which the clocks change in `zone`, forward or back."""
    found, day = [], date(year, 1, 1)
    while day.year == year:
        # Both midnights read the same way (fold=0, a skipped one with the offset before
        # the change), so a change at midnight counts once, on the day it skips.
        start = datetime.combine(day, time(0, 0), tzinfo=zone)
        end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
        if start.utcoffset() != end.utcoffset():
            found.append(day)
        day += timedelta(days=1)
    return found


def clock_change_probes() -> list[dict[str, Any]]:
    """Profiles built to land in the hours a clock change skips or repeats: six clock
    times stepping through the night (from every half hour between 21:30 and 03:30, 30 or
    45 minutes apart), and six sun-relative ones stepping up to sunrise (from 7 to 3 hours
    before it, an hour apart), each under `collapse` and under `clamp`."""

    def clock(minutes: int) -> dict[str, Any]:
        minutes %= 24 * 60
        return {"type": "clock", "time": f"{minutes // 60:02d}:{minutes % 60:02d}"}

    def sun(offset: int) -> dict[str, Any]:
        return {"type": "sun", "event": "sunrise", "offset_minutes": offset}

    shapes = [
        [clock(start + i * step) for i in range(6)]
        for start in range(21 * 60 + 30, 27 * 60 + 31, 30)
        for step in (30, 45)
    ]
    shapes += [[sun(-first + i * 40) for i in range(6)] for first in range(420, 179, -60)]
    return [
        {
            "id": f"probe_{n}_{policy}",
            "collision_policy": policy,
            "min_section_minutes": 30,
            "sections": [
                {"id": section, "trigger": trigger}
                for section, trigger in zip(CANONICAL_ORDER, triggers)
            ],
        }
        for n, triggers in enumerate(shapes)
        for policy in ("collapse", "clamp")
    ]


def sweep_clock_changes(year: int) -> list[str]:
    """Every probe profile over each clock change of `year`, and the days either side, at
    every site whose clocks change: what was unexpected (nothing, when the scheduler is
    sound across them)."""
    unexpected = []
    probes = clock_change_probes()
    for label, latitude, longitude, zone in SITES:
        tz = ZoneInfo(zone)
        days = sorted({c + timedelta(days=d) for c in clock_changes(tz, year) for d in (-1, 0, 1)})
        if not days:
            continue
        provider = AstralSunProvider(latitude, longitude, tz)
        for profile in probes:
            result = sweep_days(profile, provider, days, label)
            unexpected += [f"{label}, {profile['id']}: {u}" for u in result.unexpected]
    return unexpected


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
    probed = sweep_clock_changes(args.year)
    print(f"\nThe scheduler across every clock change of {args.year} (probe profiles)")
    print("-" * 78)
    print(f"{len(probed)} unexpected" if probed else "sound: every section run is in force")
    for line in probed[:10]:
        print(f"{'':<20} UNEXPECTED {line}")
    if len(probed) > 10:
        print(f"{'':<20} ... and {len(probed) - 10} more")
    unexpected += len(probed)
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
