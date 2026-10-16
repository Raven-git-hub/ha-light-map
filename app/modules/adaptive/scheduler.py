"""Scheduler: compute section boundaries (sun-relative + fixed) and apply collision policy.
Ported from ha-adaptive-light.

Sections come from the room's schedule profile. The scheduler is level-triggered: the
current section is a function of the time, not of having seen a crossover edge.

Port the boundary and collision logic intact; it is proven. `tools/sweep.py` runs a year of
these computations to catch unexpected section collapses.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any


def compute_boundaries(
    profile: Mapping[str, Any], day: date, location: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return one day's section boundaries for a schedule profile at a location."""
    raise NotImplementedError


def apply_collision_policy(boundaries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Resolve boundaries that collide or invert (e.g. a sun-relative boundary passing a
    fixed one)."""
    raise NotImplementedError


def section_at(profile: Mapping[str, Any], when: datetime, location: Mapping[str, Any]) -> str:
    """Return the section in force at `when`."""
    raise NotImplementedError
