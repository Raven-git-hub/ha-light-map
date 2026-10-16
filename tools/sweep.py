"""Sweep: a year of section-boundary computations, checking for unexpected collapses.

Drives `app.modules.adaptive.scheduler` across every day of a year for each schedule profile
and reports the days where sections collapse unexpectedly.
"""

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep and report collapses; return a process exit code."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
