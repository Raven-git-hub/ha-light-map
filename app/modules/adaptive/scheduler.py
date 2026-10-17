"""Scheduler: compute section boundaries (sun-relative + fixed) and apply collision policy.
Ported from ha-adaptive-light.

Pure computation: a schedule profile, a date and a place in, the day's six section
boundaries out, each one run or collapsed with its reason. No Home Assistant, no store, no
I/O, so it can be checked offline across a whole year, which matters because its failure
modes are seasonal: a rule that misbehaves in December looks perfect in July.
`tools/sweep.py` runs a year of these computations to catch unexpected collapses.

A profile (`schedule_profiles[]`, validated by `core.config`) lists the six sections in
canonical order, each with the trigger that starts it every day (a clock time, a sun event
with an offset, or the earliest or latest of several) and an optional priority, plus its
`collision_policy` and `min_section_minutes`. See docs/DESIGN.md, "Schedule profiles".

Two ways a section can collide with those already kept for the day (`_find_conflict`):
*order* (a section later in canonical order has already started: a UK midsummer sunset at
21:21 after night at 20:30) and *gap* (it starts within `min_section_minutes` of the last).
The lower priority loses and is collapsed (skipped for the day); under `clamp` a gap is
resolved by moving the later section to start `min_section_minutes` after the one before,
but an order inversion always collapses: moving sunset later still leaves it after night.

The scheduler is level-triggered: the current section is a function of the time
(`active_section_at`, `section_at`), not of having seen a crossover edge, so a start after
downtime, a dropped connection and a clock change all take the path normal running takes.

Sun times come from a provider. `AstralSunProvider` computes them locally from HA's own
location (latitude, longitude, elevation and time zone, from HA's `/api/config`), so the
container and HA agree on when sunset is; HA's `sun.sun` can't be used directly, since it
knows only the next event, not when today's sunrise was, which catching up needs.
`FixedSunProvider` supplies them explicitly, for tests.

Ported intact (the predecessor's `compute_day`, `resolve_trigger`, `default_priority`,
`_find_conflict` and `active_section_at` keep their proven API and behaviour), with two
departures, each a day the predecessor got wrong (P5.2a's review):

- A clamp never moves a section past the day's end: one that can't fit collides as under
  `collapse` instead. The predecessor moved it anyway, so with a long minimum a section
  could start after midnight, even after the next day's first boundary, and be reported as
  run yet never be in force.
- `active_section_at` compares times as instants, not wall-clock readings: where the clocks
  go back across a boundary (Beirut at midnight), the repeated hour read as before the
  boundary again, and a runtime polling it would fire the previous section a second time.

New here: `apply_collision_policy` is split out of `compute_day` (it works on copies, so a
caller's boundaries are never changed), and `compute_boundaries` and `section_at` take HA's
location instead of a provider.

Hard rules:

- Sections are core's vocabulary (`core.config.SECTIONS`); the module takes them from there.
- Importing this module does no work: `astral` is imported when a sun time is first asked
  for.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import tzinfo as TimeZone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.core.config import (
    DEFAULT_COLLISION_POLICY,
    DEFAULT_MIN_SECTION_MINUTES,
    SECTIONS,
)

CANONICAL_ORDER = {section: i for i, section in enumerate(SECTIONS)}

# A fixed time states a fact about the household's routine that holds whatever the sun does;
# a sun-relative time encodes an assumption about daylight that a collision has already
# falsified. So by default the clock wins.
DEFAULT_PRIORITY = {"clock": 100, "sun": 50}

# How far back `active_section_at` looks: before the day's first boundary the last section
# of yesterday is still in force, and a day whose every section collapsed (a polar profile)
# hands over to the one before.
LOOKBACK_DAYS = 2


class SunUnavailable(Exception):
    """No sunrise or sunset occurs on this date at this latitude (polar day or night), or
    no section has started in the days looked back over."""


# -- Sun providers ---------------------------------------------------------------------


class SunProvider(Protocol):
    def times(self, on: date) -> dict[str, datetime]: ...


@dataclass
class AstralSunProvider:
    """Sun times computed locally from a location. The runtime builds it from HA's own
    location (`provider_for`), so it agrees with HA's sun integration."""

    latitude: float
    longitude: float
    tzinfo: TimeZone
    elevation: float = 0.0

    def times(self, on: date) -> dict[str, datetime]:
        from astral import LocationInfo
        from astral import sun as astral_sun

        observer = LocationInfo(latitude=self.latitude, longitude=self.longitude).observer
        observer.elevation = self.elevation
        out: dict[str, datetime] = {}
        for key, event in (("sunrise", astral_sun.sunrise), ("sunset", astral_sun.sunset)):
            try:
                out[key] = event(observer, on, tzinfo=self.tzinfo)
            except ValueError:
                pass  # polar day or polar night: the event doesn't occur
        return out


@dataclass
class FixedSunProvider:
    """A test double: sun times given explicitly (None: the event doesn't occur)."""

    sunrise: time | None
    sunset: time | None
    tzinfo: TimeZone | None = None

    def times(self, on: date) -> dict[str, datetime]:
        out = {}
        if self.sunrise:
            out["sunrise"] = datetime.combine(on, self.sunrise, tzinfo=self.tzinfo)
        if self.sunset:
            out["sunset"] = datetime.combine(on, self.sunset, tzinfo=self.tzinfo)
        return out


def provider_for(location: Mapping[str, Any]) -> AstralSunProvider:
    """A sun provider for a location as HA's `/api/config` gives it (`latitude`,
    `longitude`, `elevation`, `time_zone`). Raises ValueError if it doesn't say where or
    in which time zone: a guess would put every sun-relative boundary somewhere else."""
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
        zone = ZoneInfo(str(location["time_zone"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Home Assistant's location is incomplete ({exc!r}): set its latitude, longitude "
            "and time zone"
        ) from None
    return AstralSunProvider(latitude, longitude, zone, float(location.get("elevation") or 0))


# -- Trigger resolution ----------------------------------------------------------------


def resolve_trigger(
    trigger: Mapping[str, Any],
    sun: Mapping[str, datetime],
    on: date,
    tzinfo: TimeZone | None = None,
) -> datetime:
    """When a trigger fires on `on`. Raises SunUnavailable when it needs a sun event that
    doesn't happen that day (for a composite: when none of its components resolves)."""
    kind = trigger["type"]

    if kind == "clock":
        hh, mm = (int(x) for x in trigger["time"].split(":"))
        return datetime.combine(on, time(hh, mm), tzinfo=tzinfo)

    if kind == "sun":
        event = trigger["event"]
        if event not in sun:
            raise SunUnavailable(f"no {event} on {on}")
        return sun[event] + timedelta(minutes=trigger.get("offset_minutes", 0))

    if kind in ("earliest", "latest"):
        resolved = []
        for sub in trigger["of"]:
            try:
                resolved.append(resolve_trigger(sub, sun, on, tzinfo))
            except SunUnavailable:
                continue
        if not resolved:
            raise SunUnavailable(f"no component of {kind} resolved on {on}")
        return min(resolved) if kind == "earliest" else max(resolved)

    raise ValueError(f"unknown trigger type: {kind}")


def default_priority(trigger: Mapping[str, Any]) -> int:
    kind = trigger["type"]
    if kind in ("earliest", "latest"):
        # A composite is as firm as its firmest component: 'sunrise or 05:30, whichever is
        # earlier' has a clock floor and should not be treated as purely sun-relative.
        return max(default_priority(sub) for sub in trigger["of"])
    return DEFAULT_PRIORITY.get(kind, 50)


# -- Boundaries ------------------------------------------------------------------------


@dataclass
class Boundary:
    """One section's start on one day: when it was planned, with what priority, and
    whether it ran or collapsed (and why). `ends` is when the next kept section starts
    (None for the day's last). `name` is the section's display name, its id until P5.2b
    gives sections names."""

    section: str
    name: str
    planned: datetime | None
    priority: int
    outcome: str = "ran"  # ran | collapsed
    reason: str | None = None
    ends: datetime | None = None

    @property
    def ran(self) -> bool:
        return self.outcome == "ran"


def compute_day(
    schedule: Mapping[str, Any], provider: SunProvider, on: date, tzinfo: TimeZone | None = None
) -> list[Boundary]:
    """Return all six sections for `on`, chronologically ordered, each marked as having run
    or collapsed with a stated reason."""
    sun = provider.times(on)
    candidates: list[Boundary] = []
    for section in schedule["sections"]:
        trigger = section["trigger"]
        priority = section.get("priority", default_priority(trigger))
        name = section.get("name", section["id"])
        try:
            planned = resolve_trigger(trigger, sun, on, tzinfo)
        except SunUnavailable as exc:
            candidates.append(
                Boundary(section["id"], name, None, priority, outcome="collapsed", reason=str(exc))
            )
            continue
        # A sun offset can push a boundary outside the day; clamp rather than let a section
        # leak into yesterday or tomorrow.
        lo = datetime.combine(on, time(0, 0), tzinfo=tzinfo)
        hi = datetime.combine(on, time(23, 59), tzinfo=tzinfo)
        planned = min(max(planned, lo), hi)
        candidates.append(Boundary(section["id"], name, planned, priority))
    return apply_collision_policy(
        candidates,
        policy=schedule.get("collision_policy", DEFAULT_COLLISION_POLICY),
        min_section_minutes=schedule.get("min_section_minutes", DEFAULT_MIN_SECTION_MINUTES),
        day_end=datetime.combine(on, time(23, 59), tzinfo=tzinfo),
    )


def apply_collision_policy(
    boundaries: Sequence[Boundary],
    *,
    policy: str = DEFAULT_COLLISION_POLICY,
    min_section_minutes: int = DEFAULT_MIN_SECTION_MINUTES,
    day_end: datetime | None = None,
) -> list[Boundary]:
    """Resolve one day's boundaries that collide or invert (e.g. a sun-relative boundary
    passing a fixed one), and return them all, chronologically, each run or collapsed with
    its reason and each run one's `ends` set. Works on copies: `boundaries` is left as it
    is. A boundary already collapsed (no planned time) stays so, and sorts at `day_end`
    (or last, without one)."""
    min_gap = timedelta(minutes=min_section_minutes)
    candidates = [dataclasses.replace(b) for b in boundaries]

    resolvable = [c for c in candidates if c.planned is not None and c.ran]
    resolvable.sort(key=lambda b: (b.planned, CANONICAL_ORDER[b.section]))

    kept: list[Boundary] = []
    for cand in resolvable:
        while True:
            blocker, violation = _find_conflict(cand, kept, min_gap)
            if blocker is None:
                break

            cand_wins = cand.priority > blocker.priority or (
                cand.priority == blocker.priority
                and CANONICAL_ORDER[cand.section] < CANONICAL_ORDER[blocker.section]
            )

            # Clamping can rescue a section that merely starts too soon, but it cannot
            # rescue one that starts in the wrong order: shifting the sunset scene later
            # still leaves it running after the night scene. Order inversions always
            # collapse.
            if policy == "clamp" and violation == "gap":
                moved = blocker.planned + min_gap
                # Never past the day's end (a departure from the predecessor): moved there, a
                # section could start after the next day's first boundary and never be in
                # force, though reported as run. One that can't fit collides as under
                # collapse, below.
                if day_end is None or moved <= day_end:
                    cand.planned = moved
                    cand.reason = f"clamped to {min_gap} after {blocker.section}"
                    break

            loser, winner = (blocker, cand) if cand_wins else (cand, blocker)
            loser.outcome = "collapsed"
            loser.reason = (
                f"{violation} conflict: {winner.section} (priority {winner.priority}) "
                f"takes precedence over {loser.section} (priority {loser.priority})"
            )
            if cand_wins:
                kept.remove(blocker)
            else:
                break

        if cand.ran:
            kept.append(cand)

    for i, b in enumerate(kept):
        b.ends = kept[i + 1].planned if i + 1 < len(kept) else None

    collapsed = [c for c in candidates if not c.ran]

    def when(b: Boundary) -> tuple[int, Any]:
        if b.planned is not None:
            return (0, b.planned)
        return (0, day_end) if day_end is not None else (1, 0)

    return sorted(kept + collapsed, key=when)


def _find_conflict(
    cand: Boundary, kept: list[Boundary], min_gap: timedelta
) -> tuple[Boundary | None, str | None]:
    """Two ways a candidate can conflict with what has already been kept.

    'order'  - a section later in canonical order has already started, so this one would
               run out of sequence. A UK midsummer sunset at 21:21 falling after night at
               20:30 is this case: the gap is legal, the ordering is not.
    'gap'    - it starts within min_section_minutes of the previous section, leaving too
               little time for the crossover transition to finish before being overridden.
    """
    inversions = [k for k in kept if CANONICAL_ORDER[k.section] > CANONICAL_ORDER[cand.section]]
    if inversions:
        return max(inversions, key=lambda k: CANONICAL_ORDER[k.section]), "order"
    if kept and cand.planned is not None and (cand.planned - kept[-1].planned) < min_gap:
        return kept[-1], "gap"
    return None, None


def _not_after(planned: datetime, when: datetime) -> bool:
    """`planned` is no later than `when`, as instants when both have a zone: Python compares
    two times in the same zone by their wall clock, ignoring `fold`, so in an hour the
    clocks repeat (a fall-back) a boundary already past would read as still to come."""
    if planned.tzinfo is not None and when.tzinfo is not None:
        return planned.timestamp() <= when.timestamp()
    return planned <= when


def active_section_at(
    schedule: Mapping[str, Any],
    provider: SunProvider,
    when: datetime,
    tzinfo: TimeZone | None = None,
) -> tuple[str, datetime]:
    """Which section should be in force at `when`, and when it started.

    Looks back a day (two, for a day whose sections all collapsed), because between midnight
    and the first boundary the section in force is the last one from yesterday. This is
    what makes the runtime level-triggered: a start after downtime catches up by asking it.
    """
    for delta in range(LOOKBACK_DAYS + 1):
        day = when.date() - timedelta(days=delta)
        ran = [b for b in compute_day(schedule, provider, day, tzinfo) if b.ran]
        past = [b for b in ran if _not_after(b.planned, when)]
        if past:
            last = past[-1]
            return last.section, last.planned
    raise SunUnavailable("could not determine an active section")


# -- The module's entry points ---------------------------------------------------------


def compute_boundaries(
    profile: Mapping[str, Any], day: date, location: Mapping[str, Any]
) -> list[Boundary]:
    """Return one day's section boundaries for a schedule profile at a location (HA's
    `/api/config`: latitude, longitude, elevation, time zone), in that time zone."""
    provider = provider_for(location)
    return compute_day(profile, provider, day, provider.tzinfo)


def section_at(profile: Mapping[str, Any], when: datetime, location: Mapping[str, Any]) -> str:
    """Return the section in force at `when` (a time with a zone) at a location."""
    provider = provider_for(location)
    return active_section_at(profile, provider, when.astimezone(provider.tzinfo), provider.tzinfo)[
        0
    ]
