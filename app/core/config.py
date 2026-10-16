"""Config: load/validate/default the config against the JSON Schema
(`schema/config.schema.json`); cross-check references; reconcile the Area snapshot.
Validate-then-write: a config that won't load must never replace one that does.

Everything downstream is a pure function of this committed document. Learned data
(almanacs) is not stored here; it lives in the almanac store.

Hard rules:

- Config writes are validate-then-write, never the reverse.
- The HA token is never persisted in the config: it is stripped on save and export and
  supplied at runtime from LM_HA_TOKEN or LM_HA_TOKEN_FILE. Whether LM_HA_BASE_URL overrides
  `homeassistant.base_url` is decided in P1.
- `version` is the schema version; the config is migrated forward on load.
- A schedule profile with id `default` must always exist.
- `adaptive_config` is required on a room when `adaptive` is true and forbidden when false.
- HA Areas are the source of truth for room membership; the config's room snapshot is
  reconciled against them, never overwritten destructively by surprise.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """The config failed to load, validate, or cross-check; the previous config stays."""


def load(path: Path) -> dict[str, Any]:
    """Read the config at `path`, migrate it forward, apply defaults, validate, and
    cross-check it."""
    raise NotImplementedError


def migrate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the config migrated forward to the current schema version."""
    raise NotImplementedError


def apply_defaults(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the config with defaults filled in."""
    raise NotImplementedError


def validate(config: Mapping[str, Any]) -> None:
    """Validate the config against the JSON Schema; raise ConfigError if it fails."""
    raise NotImplementedError


def cross_check(config: Mapping[str, Any]) -> None:
    """Check what the schema can't (ids unique, every reference resolves); raise
    ConfigError if a check fails."""
    raise NotImplementedError


def save(path: Path, config: Mapping[str, Any]) -> None:
    """Validate-then-write: strip the token, validate fully, then atomically replace the
    file at `path`."""
    raise NotImplementedError


def redact(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the config without `homeassistant.access_token`, for save and
    export."""
    raise NotImplementedError


def reconcile_area_snapshot(
    config: Mapping[str, Any],
    areas: Sequence[Mapping[str, Any]],
    lights: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the config with its room snapshot reconciled against HA's Areas and lights."""
    raise NotImplementedError
