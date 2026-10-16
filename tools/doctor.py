"""Doctor: probe a real HA instance; validate connectivity and entities.

Run by hand against a live Home Assistant; the connection comes from LM_HA_BASE_URL and
LM_HA_TOKEN (or LM_HA_TOKEN_FILE).
"""

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Probe HA and report problems; return a process exit code."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
