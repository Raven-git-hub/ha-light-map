"""Composition root: the FastAPI app wiring core + modules, plus HTTP endpoints.

The entry point (`uvicorn app.main:app`). `python -c "import app.main"` must succeed, so
nothing here may contact Home Assistant or do work at import time: `create_app` builds the
app without I/O, and the HA client is made in the app's lifespan (from LM_HA_BASE_URL and
LM_HA_TOKEN / LM_HA_TOKEN_FILE), which opens no connection until a request needs one.
Tests pass their own client (`create_app(ha=...)`) and a config path.

Core never imports a module: modules are attached here, through the four seams described
in `app.modules`, so a module can be replaced or added without touching the trunk. The
seams are registries filled here: a module's namespace (`core.generate.register_namespace`),
its bridge brightness (`core.switching.register_bridge_brightness`), and its observation
(`Observer.subscribe`, `register_guard`, `register_section`). The core's own `lm` lane
registers itself. Adaptive Light's config and editor are in (P5.0), and so are its generator
(P5.1) and its engine (P5.2a), but the module is wired in only by the cutover switch,
`ADAPTIVE_ENABLED`, which is off until P6 (see `wire_modules`): its generator and bridge
brightness here, and its runtime (which hooks into the observer: the section, the guard,
the observations) in the lifespan, only when the module is wired.

The lifespan also opens the store (the CSV archive and its query layer, beside the config
unless `system.data_dir` says otherwise) and starts the observer (P4), which subscribes to
HA's events once the config has a room with bulbs, and, with the adaptive module wired in,
its runtime (`app.modules.adaptive.runtime`: crossovers, the nightly analysis, the
almanac's publish). Every config save tells the observer to re-read the config (the runtime
re-reads it every tick). Without HA, or with a data directory that can't be opened, the Map
still serves, and `/api/status` says what is missing.

The HTTP surface is the Map tab's (P3), the Log tab's (P4) and the adaptive editor's
(the Config tab, P5.0). Everything under /api/
answers JSON; an error is `{error, detail, ...}` with `problems` when there are several.
Every request first passes the Host allowlist (`LM_ALLOWED_HOSTS`, plus `localhost` and
`127.0.0.1`), which refuses a DNS-rebinding page; it is not authentication.

- Reads: `GET /api/status`, `/api/map` (the Map's view of the committed config),
  `/api/config` (the config itself, token-free), `/api/entities` (HA entities a hard
  switch can take as its source), `/api/log` (the event log, newest first, by minimum
  level, category and room), `/api/rooms/{room}/adaptive_config` (the adaptive editor's
  view of a room: its block, each active bulb's tier, unit and dimmability; the one read
  of HA's states it makes never fails it).
- Config edits, each validate-then-write: `PUT /api/rooms/{room}/adaptive`,
  `PUT /api/rooms/{room}/adaptive_config` (the adaptive editor's save, P5.0),
  `PUT /api/bulbs/{bulb}/hard_switches`, `PUT /api/bulbs/{bulb}/ignored` (set a light that
  isn't a room bulb aside, or bring it back; P4.1), `POST /api/rooms/{room}/groups`,
  `PUT|DELETE /api/rooms/{room}/groups/{group}`. Each edit of something that exists
  carries the `version` the Map showed for it as `base`, and is refused (409
  `edit_stale`) if it has changed since: a save from a stale tab never silently reverts
  another edit, and an acknowledgement or confirmation covers only what was shown.
- Area edits, which change HA first (verified) and then commit exactly that change to the
  snapshot: `POST /api/rooms` (create the Area), `PUT /api/rooms/{room}/name` (rename it),
  `POST /api/bulbs/{bulb}/move/preview` then `.../move` (reassign the bulb's Area).
- Previewed actions, each a `GET|POST .../preview` that writes nothing and returns a
  `fingerprint`, then a `POST` that must carry it and is refused (409 `preview_stale`) if
  what it would do has changed since: `/api/sync` (save the snapshot re-read from HA's
  Areas), `/api/deploy` (the switching plane's `lm_` lane), `/api/areas` (write the
  snapshot back to HA's Areas). So nothing is ever applied that wasn't shown first. With
  the cutover switch on, the deploy preview also shows each module lane's plan, read-only:
  only the `lm_` lane is applied from here until cutover.

Hard rules:

- Config writes are validate-then-write (`config.save`). A move checks its config
  (`config.checked`) before its HA write, so the config write after it can't be the step
  that fails on validation; creating or renaming a room adds or renames one empty room,
  which can't invalidate the config, and is validated before it is saved. A config on disk
  that doesn't load is never overwritten from here: every write is refused until it is
  fixed by hand, and nothing is written to HA from the Map before the first reviewed sync
  has saved one.
- Area writes are previewed, verified, and never destructive by surprise: a Map edit
  commits only its own change to the snapshot; anything else HA changed waits for a sync,
  whose changes are shown before they are saved. A move never writes a light whose
  registry entry isn't the bulb's (its `ha_match_key`, compared, never addressed), and an
  Area write that stops partway says which moves landed (502 `areas_partial`) rather than
  reporting a refusal.
- The deploy preview is mandatory before apply (the fingerprint), and the switching
  deploy and the Area writes are separate actions.
- Coexistence (until cutover, P6): binding a hard switch makes its bulbs' bridges live
  beside ha-adaptive-light, so an edit that makes a hard switch drive something new is
  refused (409 `coexistence`) until the request acknowledges it (`acknowledge_live`).
  The adaptive lane's `al_` names are the live system's own, so the adaptive module stays
  unwired (`ADAPTIVE_ENABLED` off) until cutover: nothing generates, previews or writes
  `al_`, no adaptive runtime runs (nothing fires a scene or publishes an almanac), and the
  switching plane's output is exactly P5.0's.
- The HA token never reaches the config or a response.
- What changes HA or the config is recorded in the event log; a log entry that can't be
  written never fails the action it records.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core import config as config_mod
from app.core import deploy, inventory, switching
from app.core import generate as generate_mod
from app.core.config import ConfigError
from app.core.deploy import DeployError, StalePreview
from app.core.ha import HAAuthError, HAClient, HAError
from app.core.inventory import InventoryError, NotFound
from app.core.observe import Observer
from app.core.store import LEVELS, Store
from app.modules.adaptive import generate as adaptive_generate
from app.modules.adaptive.runtime import AdaptiveRuntime

log = logging.getLogger(__name__)

# -- The cutover switch ----------------------------------------------------------------
#
# Off until cutover (P6). Adaptive Light's artefacts keep the predecessor's `al_` names,
# and those are exactly what the live ha-adaptive-light holds on the same HA, down to its
# helpers' names (`AL Active <room>`, ...). An `al_` deploy now would adopt the old
# system's helpers, rewrite its scene and watchdog automations and remove its maintenance
# ones, since a lane removes whatever under its prefix the config doesn't call for.
#
# Off: neither the `al` namespace nor the adaptive bridge-brightness provider is
# registered, so `core.generate.namespaces()` is ["lm"], every bridge takes the plain path,
# and the generated `lm_` lane is byte-identical to P5.0's (tests/golden/lm_only.yaml).
# On: `al` is generated, reconciled as its own lane by `core.deploy`, and previewed
# (read-only) beside the switching deploy, adaptive bulbs' bridges read the almanac, and the
# lifespan starts the adaptive runtime (crossovers, the nightly analysis, the almanac's
# publish; P5.2a). Off, no runtime is made at all.
#
# A constant, not an environment variable: turning it on is a reviewed change made with the
# cutover, never a deployment setting that could be set early. Tests turn it on in a scope
# of their own (`tests.fakes.adaptive_wired`). Do not turn it on here before P6.
ADAPTIVE_ENABLED = False

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Where the committed config lives. The container keeps it on its /data volume.
CONFIG_PATH_ENV = "LM_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path("/data/config.json")

# The Host names the API answers, besides the loopback ones. A DNS-rebinding page points a
# name it controls at this container, which makes its scripts same-origin with the API;
# the Host header still carries that name, so an allowlist turns them away. IPv6 literals
# can't be listed: Starlette's check cuts the header at its first colon.
ALLOWED_HOSTS_ENV = "LM_ALLOWED_HOSTS"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")

# The entities the hard-switch picker offers as a state source. Anything else can still be
# typed in; Light Map's own (`lm_`) entities are left out, as the config refuses them.
SOURCE_DOMAINS = ("binary_sensor", "event", "input_boolean", "light", "switch")

# The binary sensors the adaptive editor suggests as presence sensors.
PRESENCE_CLASSES = ("motion", "occupancy", "presence")

COEXISTENCE_WARNING = (
    "Binding a hard switch makes its bulbs live: once deployed, pressing it drives them "
    "through Light Map's bridges. Until cutover (P6) Light Map runs beside "
    "ha-adaptive-light, which keeps driving the same bulbs, and neither knows what the "
    "other did. Bind only a switch the old system doesn't use. Send acknowledge_live to "
    "save it anyway."
)

MODULE_LANES_NOTE = (
    "Light Map's own module lanes, shown read-only: this deploy applies the lm_ lane only. "
    "The al_ lane holds the names the live ha-adaptive-light uses, so it is applied at "
    "cutover (P6), once the old system is gone."
)

COEXISTENCE_NOTE = (
    "Light Map runs beside ha-adaptive-light until cutover (P6). Deploying never drives a "
    "bulb: each soft switch is set to its bulb's current on/off before its bridge goes "
    "live, and a bridge acts only when its soft switch flips. A bulb with a hard switch is "
    "live: once deployed, pressing the switch drives the bulb through Light Map, while the "
    "old system still drives it too."
)

AREAS_NOTE = (
    "This writes the committed snapshot back to Home Assistant's Areas. Edits made from "
    "the Map already reach HA first, so a difference here usually means the bulb was moved "
    "in HA since the last sync, and writing the snapshot reverts that. To keep HA's "
    "version instead, use Sync from HA."
)


class ApiError(Exception):
    """An error answered as JSON: `{error, detail, **extra}` with HTTP `status`."""

    def __init__(self, status: int, error: str, detail: str, **extra: Any) -> None:
        self.status = status
        self.error = error
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


# -- The committed config --------------------------------------------------------------


class ConfigStore:
    """The committed config on disk.

    Read fresh for every request: it is small, and a hand edit is picked up at once with no
    cache to go stale. Written only through `config.save` (validate-then-write), one edit
    at a time: `lock` is held across each read-modify-write, HA writes included, so two
    edits can't interleave. Before the first save there is no file, and the config is
    `config.blank()`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = asyncio.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return config_mod.loads(config_mod.blank())
        try:
            return config_mod.load(self.path)
        except ConfigError as exc:
            # Refusing every write keeps a hand edit with a typo from being replaced by
            # whatever the Map would have written over it.
            raise ApiError(
                503,
                "config_broken",
                f"The config at {self.path} does not load, so nothing is written until it "
                "is fixed by hand.",
                problems=exc.problems,
            ) from exc

    def write(self, doc: Mapping[str, Any]) -> dict[str, Any]:
        """Validate-then-write; return what was saved."""
        saved = config_mod.checked(doc)
        try:
            config_mod.save(self.path, saved)
        except OSError as exc:
            raise ApiError(
                500, "config_write_failed", f"could not write {self.path}: {exc.strerror}"
            ) from exc
        return saved


def fingerprint(value: Any) -> str:
    """A digest of what a preview showed: an apply carrying it is refused if what it would
    do now digests differently. Generation is deterministic, so an unchanged plan always
    digests the same."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- Request bodies --------------------------------------------------------------------


class Confirm(BaseModel):
    fingerprint: str


class RoomName(BaseModel):
    name: str


class AdaptiveFlag(BaseModel):
    adaptive: bool
    discard_adaptive_config: bool = False
    base: str


class IgnoredFlag(BaseModel):
    ignored: bool
    confirm_drops: bool = False
    base: str


class AdaptiveConfigEdit(BaseModel):
    # The whole block, checked by the config's own JSON Schema and cross-check when saved
    # (one definition of its shape: schema/config.schema.json), and normalised by the
    # defaults (the scene matrix reshaped to the units).
    adaptive_config: dict[str, Any]
    base: str


class Move(BaseModel):
    room_id: str | None


class MoveConfirm(Move):
    fingerprint: str


class HardSwitches(BaseModel):
    # Bindings are checked by the config's own JSON Schema and cross-check when saved, so
    # their shape has one definition (schema/config.schema.json), not a second one here.
    hard_switches: list[dict[str, Any]]
    acknowledge_live: bool = False
    base: str


class Group(BaseModel):
    name: str
    members: list[str]
    hard_switches: list[dict[str, Any]] = Field(default_factory=list)
    acknowledge_live: bool = False
    base: str | None = None  # required when editing an existing group


# -- Wiring ----------------------------------------------------------------------------


def _store(request: Request) -> ConfigStore:
    return request.app.state.store


def _ha(request: Request) -> HAClient:
    ha = request.app.state.ha
    if ha is None:
        raise ApiError(
            503,
            "ha_not_configured",
            f"Home Assistant is not configured: {request.app.state.ha_error}",
        )
    return ha


def _write(request: Request, doc: Mapping[str, Any], *, after_ha: str | None = None) -> Any:
    """Save the config. `after_ha` says what was already written to HA, so a failed save
    says the HA change stands and a sync will pick it up."""
    try:
        saved = _store(request).write(doc)
    except (ApiError, ConfigError) as exc:
        if after_ha is None:
            raise
        raise ApiError(
            500,
            "saved_in_ha_only",
            f"{after_ha} in Home Assistant, but the snapshot could not be saved ({exc}). "
            "Sync from HA to bring it up to date.",
        ) from exc
    observer = request.app.state.observer
    if observer is not None:
        observer.reload()  # a first sync starts it; a moved bulb is watched in its new room
    return saved


def _record(
    request: Request,
    category: str,
    message: str,
    *,
    room_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Note an action in the event log. Never raises: the action has already happened,
    and failing to log it must not report it as failed."""
    data = request.app.state.data
    if data is None:
        return
    try:
        data.log_event(level, category, message, room_id=room_id, detail=detail)
    except Exception:  # noqa: BLE001
        log.exception("could not write to the event log")


def _check_base(seen: str | None, current: str, what: str, view: str = "the Map") -> None:
    """Refuse an edit made against a `version` other than the current one: another tab, or
    a sync, changed the thing after the Map (or the adaptive editor: `view`) showed it.
    Edits replace whole lists (a bulb's hard switches, a group's members, a room's adaptive
    config), so saving the stale view would silently undo the other change, and an
    acknowledgement or confirmation given for the old view would cover what the user never
    saw."""
    if seen != current:
        raise ApiError(
            409,
            "edit_stale",
            f"{what} has changed since {view} showed it (in another tab, or by a sync): "
            f"reload {view} and make the edit again.",
        )


# The `version` the Map shows for each editable thing, which an edit sends back as `base`.


def bulb_version(bulb: Mapping[str, Any]) -> str:
    """A bulb's hard switches, and whether it is ignored (a popup for a bulb since set
    aside is stale): what an edit of its hard switches is made against."""
    return fingerprint(
        {"hard_switches": bulb.get("hard_switches", []), "ignored": inventory.is_ignored(bulb)}
    )


def ignore_version(doc: Mapping[str, Any], bulb: Mapping[str, Any]) -> str:
    """What ignoring a bulb (or un-ignoring it) is made against: its hard switches,
    whether it is ignored, and what ignoring it would drop (its memberships, and any group
    or unit that would go with it), so a confirmation covers exactly the list the Map
    showed. Apart from `bulb_version`, so a group renamed in another tab doesn't make an
    open hard-switch popup stale."""
    return fingerprint(
        {
            "bulb": bulb_version(bulb),
            "drops": inventory.ignore_drops(doc, bulb["id"]),
        }
    )


def group_version(group: Mapping[str, Any]) -> str:
    return fingerprint({k: group.get(k) for k in ("name", "members", "hard_switches")})


def room_version(room: Mapping[str, Any]) -> str:
    return fingerprint({"adaptive": room.get("adaptive"), "config": room.get("adaptive_config")})


def editor_version(doc: Mapping[str, Any], room: Mapping[str, Any]) -> str:
    """What the adaptive editor's save is made against: the room's adaptive flag and block
    (`room_version`) and its active-bulb roster. A bulb ignored, moved out or synced in from
    another tab while the editor is open changes which bulbs the editor may offer without
    always touching the block (a switched-only bulb's doesn't), so with the block alone a
    stale editor that then made an ignored bulb adaptive got a validation error (422) the
    user couldn't act on, rather than a reload (409 `edit_stale`)."""
    roster = sorted(
        bulb["id"]
        for home, bulb in inventory.active_bulbs(doc)
        if home is not None and home["id"] == room["id"]
    )
    return fingerprint({"room": room_version(room), "bulbs": roster})


def _commit(
    request: Request,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    acknowledge: bool,
    *,
    record: str,
    room_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
    category: str = "map",
) -> dict[str, Any]:
    """Validate a config edit, refuse it while it makes a hard switch drive something new
    unless that is acknowledged, then save it and note it (`record`, with `detail`, under
    `category`) in the event log. Returns what it made live."""
    checked = config_mod.checked(after)
    live = inventory.going_live(before, checked)
    if live and not acknowledge:
        raise ApiError(409, "coexistence", COEXISTENCE_WARNING, going_live=live)
    _write(request, checked)
    noted = dict(detail or {})
    if live:
        record += " It makes a hard switch drive something new (acknowledged)."
        noted["going_live"] = live
    _record(request, category, record, room_id=room_id, detail=noted or None)
    return {"saved": True, "going_live": live}


# -- Views -----------------------------------------------------------------------------


def map_view(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The Map's view of the committed config: rooms by name, then Unassigned; each bulb
    with its soft switch, whether it is live (a hard switch drives it, directly or through
    a switching group), and what ignoring it would drop; each room's groups. The ignored
    bulbs are listed apart (`ignored`, rooms in the same order, then Unassigned), and are
    in no room's or Unassigned's `bulbs`: those are what the rows and the pickers offer."""
    live = inventory.live_bulbs(doc)
    targets = inventory.hard_switch_targets(doc)

    def bulb_view(bulb: Mapping[str, Any], groups_of: Mapping[str, list[str]]) -> dict[str, Any]:
        bulb_id = bulb["id"]
        return {
            "id": bulb_id,
            "name": bulb["name"],
            "entity_id": bulb["entity_id"],
            "version": bulb_version(bulb),
            "ignore_version": ignore_version(doc, bulb),
            "ignore_drops": inventory.ignore_drops(doc, bulb_id),
            "soft": switching.soft_entity(bulb_id),
            "hard_switches": bulb.get("hard_switches", []),
            "groups": groups_of.get(bulb_id, []),
            "live": bulb_id in live,
            "live_via": [
                {
                    "id": binding_id,
                    "name": targets[binding_id]["binding"].get("name"),
                    "owner": targets[binding_id]["owner"],
                }
                for binding_id in live.get(bulb_id, [])
            ],
        }

    def ignored_view(room: Mapping[str, Any] | None, bulb: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": bulb["id"],
            "name": bulb["name"],
            "entity_id": bulb["entity_id"],
            "ignore_version": ignore_version(doc, bulb),
            "room_id": room["id"] if room else None,
            "room_name": room["name"] if room else inventory.UNASSIGNED,
        }

    def active(bulbs: Any) -> list[Mapping[str, Any]]:
        return [b for b in bulbs if not inventory.is_ignored(b)]

    rooms = []
    ignored = []
    for room in sorted(doc.get("rooms", []), key=lambda r: (r["name"].casefold(), r["id"])):
        ignored += [ignored_view(room, b) for b in room.get("bulbs", []) if inventory.is_ignored(b)]
        groups = room.get("switching_groups", [])
        groups_of: dict[str, list[str]] = {}
        for group in groups:
            for member in group["members"]:
                groups_of.setdefault(member, []).append(group["id"])
        rooms.append(
            {
                "id": room["id"],
                "name": room["name"],
                "ha_area_id": room["ha_area_id"],
                "version": room_version(room),
                "adaptive": room["adaptive"],
                "adaptive_config": room.get("adaptive_config"),
                "adaptive_drops": inventory.adaptive_config_content(room),
                "bulbs": [bulb_view(b, groups_of) for b in active(room.get("bulbs", []))],
                "ignored_count": len(room.get("bulbs", [])) - len(active(room.get("bulbs", []))),
                "groups": [
                    {
                        "id": g["id"],
                        "version": group_version(g),
                        "name": g["name"],
                        "members": g["members"],
                        "hard_switches": g.get("hard_switches", []),
                        "live": bool(g.get("hard_switches")),
                    }
                    for g in groups
                ],
            }
        )
    unassigned = doc.get("unassigned", [])
    ignored += [ignored_view(None, b) for b in unassigned if inventory.is_ignored(b)]
    return {
        "rooms": rooms,
        "unassigned": {
            "name": inventory.UNASSIGNED,
            "bulbs": [bulb_view(b, {}) for b in active(unassigned)],
            "ignored_count": len(unassigned) - len(active(unassigned)),
        },
        "ignored": ignored,
        "live": {"bulbs": sorted(live), "hard_switches": len(targets)},
    }


def adaptive_view(
    doc: Mapping[str, Any],
    room: Mapping[str, Any],
    states: Mapping[str, Mapping[str, Any]] | None,
    ha_error: str | None,
) -> dict[str, Any]:
    """The adaptive editor's view of one adaptive room (P5.0): its adaptive_config as
    saved (normalised), the `version` a save sends back as `base` (`editor_version`, which
    also covers the room's active bulbs), and each active bulb
    with its tier, its learning unit, its switching groups and its dimmability
    (`inventory.bulb_capability`, from `states`: entity_id -> HA state, or None when HA
    couldn't be read, `ha_error` saying why, which leaves every capability unknown and so
    nothing locked). Also the room's switching groups ("learn as one unit"), the schedule
    profiles to choose from, the sections, and sensor suggestions from the same states.

    Bulbs come through `inventory.active_bulbs`: an ignored bulb is never offered."""
    adaptive_config = room["adaptive_config"]
    adaptive = set(adaptive_config.get("adaptive_bulbs", []))
    unit_of = {m: u["id"] for u in adaptive_config.get("learning_units", []) for m in u["members"]}
    groups = room.get("switching_groups", [])
    bulbs = []
    for home, bulb in inventory.active_bulbs(doc):
        if home is None or home["id"] != room["id"]:
            continue
        if states is None:
            capability = inventory.capability_unknown(f"Capability unknown: {ha_error}.")
        else:
            capability = inventory.bulb_capability(states.get(bulb["entity_id"]))
        bulbs.append(
            {
                "id": bulb["id"],
                "name": bulb["name"],
                "entity_id": bulb["entity_id"],
                "tier": "adaptive" if bulb["id"] in adaptive else "switched",
                "unit": unit_of.get(bulb["id"]),
                "groups": [g["id"] for g in groups if bulb["id"] in g["members"]],
                "capability": capability,
            }
        )
    return {
        "room": {k: room[k] for k in ("id", "name", "ha_area_id")},
        "version": editor_version(doc, room),
        "adaptive_config": adaptive_config,
        "sections": list(config_mod.SECTIONS),
        "modes": list(config_mod.MODES),
        "bulbs": bulbs,
        "groups": [{k: g[k] for k in ("id", "name", "members")} for g in groups],
        "schedule_profiles": [
            {"id": p["id"], "name": p.get("name") or p["id"]}
            for p in doc.get("schedule_profiles", [])
        ],
        "suggestions": _sensor_suggestions(states or {}),
        "ha": {"available": states is not None, "error": ha_error},
    }


def _sensor_suggestions(states: Mapping[str, Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Entities to suggest as a room's lux and presence sensors, by what HA says they are.
    Only suggestions: any entity id the config accepts can be typed."""
    found: dict[str, list[dict[str, Any]]] = {"lux_sensors": [], "presence_sensors": []}
    for entity_id, state in sorted(states.items()):
        domain, _, object_id = entity_id.partition(".")
        if object_id.startswith(config_mod.OWN_PREFIXES):
            continue
        attributes = state.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        entry = {"entity_id": entity_id, "name": attributes.get("friendly_name") or entity_id}
        if domain == "sensor" and (
            attributes.get("device_class") == "illuminance"
            or attributes.get("unit_of_measurement") in ("lx", "lux")
        ):
            found["lux_sensors"].append(entry)
        elif domain == "binary_sensor" and attributes.get("device_class") in PRESENCE_CLASSES:
            found["presence_sensors"].append(entry)
    return found


def _room(doc: Mapping[str, Any], room_id: str) -> Mapping[str, Any]:
    room = next((r for r in doc.get("rooms", []) if r["id"] == room_id), None)
    if room is None:
        raise NotFound(f"no room '{room_id}'")
    return room


def _group(room: Mapping[str, Any], group_id: str) -> Mapping[str, Any]:
    group = next((g for g in room.get("switching_groups", []) if g["id"] == group_id), None)
    if group is None:
        raise NotFound(f"room '{room['id']}' has no switching group '{group_id}'")
    return group


def _problems(doc: Mapping[str, Any]) -> list[str]:
    try:
        config_mod.checked(doc)
    except ConfigError as exc:
        return exc.problems
    return []


router = APIRouter(prefix="/api")


# -- Reads -----------------------------------------------------------------------------


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    """The config file and the HA connection as configured. Contacts nothing."""
    store = _store(request)
    problems: list[str] = []
    try:
        store.read()
    except ApiError as exc:
        problems = exc.extra.get("problems", [])
    state = request.app.state
    data: Store | None = state.data
    return {
        "config": {"path": str(store.path), "exists": store.exists(), "problems": problems},
        "ha": {"configured": state.ha is not None, "error": state.ha_error},
        "store": {
            "open": data is not None,
            "path": str(data.data_dir) if data is not None else None,
            "error": state.data_error,
            **(data.summary() if data is not None else {}),
        },
        "observer": (
            state.observer.status()
            if state.observer is not None
            else {"running": False, "reason": state.observer_reason}
        ),
        "coexistence": COEXISTENCE_NOTE,
        # Whether the cutover switch has wired the adaptive lane in (off until P6).
        "adaptive": {"wired": adaptive_wired_in()},
        # Its runtime, only when there is one.
        **({"adaptive_runtime": state.runtime.status()} if state.runtime is not None else {}),
    }


@router.get("/map")
def get_map(request: Request) -> dict[str, Any]:
    store = _store(request)
    view = map_view(store.read())
    return {**view, "config": {"path": str(store.path), "exists": store.exists()}}


@router.get("/config")
def get_config(request: Request) -> dict[str, Any]:
    """The committed config, as an export: the token is never in it."""
    return config_mod.redact(_store(request).read())


@router.get("/entities")
async def entities(request: Request) -> dict[str, Any]:
    """HA entities a hard switch can take as a state source, for the picker. `mapped`
    marks a bulb Light Map drives (a toggle may not take one: it would toggle itself).
    Ignored lights are left out, as everywhere the Map offers bulbs; one can still be
    typed in, though not as a toggle's source (see `config._hard_switch_loops`)."""
    ha = _ha(request)
    doc = _store(request).read()
    mapped = set(switching.references(doc)["bulbs"])
    ignored = {bulb["entity_id"] for _, bulb in inventory.ignored_bulbs(doc)}
    found = []
    for state in await ha.get_states():
        entity_id = str(state.get("entity_id", ""))
        domain, _, object_id = entity_id.partition(".")
        if domain not in SOURCE_DOMAINS or object_id.startswith("lm_") or entity_id in ignored:
            continue
        found.append(
            {
                "entity_id": entity_id,
                "name": (state.get("attributes") or {}).get("friendly_name") or entity_id,
                "domain": domain,
                "state": state.get("state"),
                "mapped": entity_id in mapped,
            }
        )
    return {"entities": sorted(found, key=lambda e: e["entity_id"])}


@router.get("/log")
def event_log(
    request: Request,
    level: str = "info",
    category: str | None = None,
    room: str | None = None,
    # Bounded so an id past SQLite's integers is a 422, not a 500.
    before: int | None = Query(None, ge=1, le=2**63 - 1),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """The event log, newest first: entries at `level` or above, optionally one
    `category` and one `room`, older than the entry `before` (paging), at most `limit`.
    Also the categories and rooms there are to filter by, with the rooms' names."""
    data: Store | None = request.app.state.data
    if data is None:
        raise ApiError(
            503,
            "store_unavailable",
            f"The event log is not available: {request.app.state.data_error}",
        )
    if level not in LEVELS:
        raise ApiError(422, "bad_request", f"level must be one of {', '.join(LEVELS)}")
    entries = data.events(
        level=level, category=category or None, room_id=room or None, before=before, limit=limit
    )
    facets = data.event_facets()
    try:
        names = {r["id"]: r["name"] for r in _store(request).read().get("rooms", [])}
    except ApiError:
        names = {}  # a broken config doesn't hide the log
    rooms = sorted(set(facets["rooms"]) | set(names), key=lambda r: (names.get(r, r).lower(), r))
    return {
        "entries": [{**e, "room_name": names.get(e["room_id"])} for e in entries],
        "levels": list(LEVELS),
        "categories": facets["categories"],
        "rooms": [{"id": r, "name": names.get(r, r)} for r in rooms],
        "more": len(entries) == limit,
    }


# -- Sync from HA ----------------------------------------------------------------------


async def _sync_proposal(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    doc = _store(request).read()
    return doc, await inventory.sync_snapshot(_ha(request), doc)


@router.get("/sync/preview")
async def sync_preview(request: Request) -> dict[str, Any]:
    """What a sync from HA's Areas would change in the snapshot. Writes nothing.

    `state`: `synced` (HA matches the snapshot), `stale` (it doesn't), or `unsaved` (no
    config has been saved yet: the first sync creates it)."""
    doc, proposed = await _sync_proposal(request)
    changes = config_mod.snapshot_changes(doc, proposed)
    exists = _store(request).exists()
    return {
        "state": "unsaved" if not exists else "stale" if changes else "synced",
        "changes": changes,
        "problems": _problems(proposed),
        "fingerprint": fingerprint({"base": doc, "proposed": proposed}),
    }


@router.post("/sync")
async def sync(request: Request, body: Confirm) -> dict[str, Any]:
    """Save the snapshot re-read from HA, exactly as previewed."""
    async with _store(request).lock:
        doc, proposed = await _sync_proposal(request)
        if fingerprint({"base": doc, "proposed": proposed}) != body.fingerprint:
            raise _stale("HA's Areas, or the config, have changed since the preview")
        _write(request, proposed)
        log.info("saved the room snapshot synced from HA")
        changes = config_mod.snapshot_changes(doc, proposed)
        _record(
            request,
            "sync",
            f"Saved the snapshot synced from Home Assistant: {len(changes)} change(s).",
            detail={"changes": changes},
        )
        return {"saved": True, "changes": changes}


def _stale(what: str) -> ApiError:
    return ApiError(409, "preview_stale", f"{what}: review the preview again.")


# -- Rooms -----------------------------------------------------------------------------


@router.post("/rooms", status_code=201)
async def create_room(request: Request, body: RoomName) -> dict[str, Any]:
    """Create a room by creating its HA Area (verified), then commit that room alone to the
    snapshot. Refused (409 `sync_first`) before the first sync has saved a config: that
    would save a config holding only this room, skipping the review of everything else."""
    ha = _ha(request)
    async with _store(request).lock:
        doc = _store(request).read()
        if not _store(request).exists():
            raise ApiError(
                409,
                "sync_first",
                "No config has been saved yet: sync from HA first, so the rooms it builds "
                "are reviewed before anything is saved.",
            )
        area = await inventory.create_room(ha, body.name)
        proposed = inventory.snapshot_with_area(doc, area)
        _write(request, proposed, after_ha=f"Area {area['name']!r} was created")
        log.info("created Area %s", area["area_id"])
        room = next(r for r in proposed["rooms"] if r["ha_area_id"] == area["area_id"])
        _record(
            request,
            "map",
            f"Created the room {area['name']} (HA Area {area['area_id']}).",
            room_id=room["id"],
        )
        return {"room": {k: room[k] for k in ("id", "name", "ha_area_id")}}


@router.put("/rooms/{room_id}/name")
async def rename_room(request: Request, room_id: str, body: RoomName) -> dict[str, Any]:
    """Rename a room by renaming its HA Area (verified); the room's id never changes."""
    ha = _ha(request)
    async with _store(request).lock:
        doc = _store(request).read()
        room = _room(doc, room_id)
        area = await inventory.rename_room(ha, room["ha_area_id"], body.name)
        proposed = inventory.snapshot_with_area(doc, area)
        _write(request, proposed, after_ha=f"Area '{area['area_id']}' was renamed")
        log.info("renamed Area %s", area["area_id"])
        _record(
            request, "map", f"Renamed {room['name']} to {area['name']}.", room_id=room_id
        )
        return {"room": {"id": room_id, "name": area["name"]}}


@router.put("/rooms/{room_id}/adaptive")
async def set_adaptive(request: Request, room_id: str, body: AdaptiveFlag) -> dict[str, Any]:
    """Flag a room adaptive (it gets a minimal adaptive_config, every bulb switched-only
    until the adaptive editor says otherwise) or not. Unflagging a room whose adaptive_config holds anything is refused (409
    `confirm_required`, with what would be dropped) unless `discard_adaptive_config`; the
    `base` check makes that confirmation cover exactly what the Map listed."""
    async with _store(request).lock:
        doc = _store(request).read()
        room = _room(doc, room_id)
        _check_base(body.base, room_version(room), f"Room {room['name']}")
        drops = inventory.adaptive_config_content(room)
        if not body.adaptive and drops and not body.discard_adaptive_config:
            raise ApiError(
                409,
                "confirm_required",
                f"Unflagging {room['name']} drops its adaptive config: {'; '.join(drops)}.",
                drops=drops,
            )
        return _commit(
            request,
            doc,
            inventory.set_adaptive(doc, room_id, body.adaptive),
            False,
            record=f"{'Flagged' if body.adaptive else 'Unflagged'} {room['name']} as adaptive.",
            room_id=room_id,
        )


def _adaptive_room(doc: Mapping[str, Any], room_id: str) -> Mapping[str, Any]:
    room = _room(doc, room_id)
    if room.get("adaptive") is not True:
        raise ApiError(
            409,
            "not_adaptive",
            f"{room['name']} isn't adaptive: tick Adaptive for it on the Map first.",
        )
    return room


async def _editor_states(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    """HA's states by entity_id, read once, for the adaptive editor's dimmability; or None
    and why not. Never fails the read: without HA the editor still works, with every
    bulb's capability unknown (and so nothing locked). That includes an answer that isn't
    a list of states (an empty body, a proxy's login page) and a failure the client didn't
    wrap: the editor edits the config, and HA is only a hint for it."""
    ha = request.app.state.ha
    if ha is None:
        return None, f"Home Assistant is not configured ({request.app.state.ha_error})"
    try:
        states = await ha.get_states()
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the editor still serves
        if not isinstance(exc, HAError):
            log.warning("reading HA's states for the adaptive editor failed: %r", exc)
        return None, f"Home Assistant could not be read ({str(exc) or type(exc).__name__})"
    if not isinstance(states, list):
        return None, "Home Assistant could not be read (its answer was not a list of states)"
    found = {}
    for state in states:
        if isinstance(state, Mapping) and isinstance(state.get("entity_id"), str):
            found[state["entity_id"]] = state
    return found, None


@router.get("/rooms/{room_id}/adaptive_config")
async def get_adaptive_config(request: Request, room_id: str) -> dict[str, Any]:
    """The adaptive editor's view of an adaptive room (`adaptive_view`). Reads HA's states
    once, for each bulb's dimmability; writes nothing. 409 `not_adaptive` for a room that
    isn't adaptive (there is no block to edit)."""
    doc = _store(request).read()
    room = _adaptive_room(doc, room_id)
    states, ha_error = await _editor_states(request)
    return adaptive_view(doc, room, states, ha_error)


@router.put("/rooms/{room_id}/adaptive_config")
async def save_adaptive_config(
    request: Request, room_id: str, body: AdaptiveConfigEdit
) -> dict[str, Any]:
    """Replace an adaptive room's whole adaptive_config, from the adaptive editor (P5.0).

    Made against the `version` the editor showed (`base`: the room's block and its active
    bulbs, `editor_version`; 409 `edit_stale` otherwise, so a stale editor never reverts
    another edit, nor offers a bulb ignored since), validated before it is written (the
    units must partition exactly the adaptive bulbs, the matrix cover them), and recorded
    in the event log with what it changed. Never reads or writes HA: whether a bulb can dim
    is the editor's guard, and the config can't see HA. It changes only the config; what
    reaches HA is the adaptive lane's next deploy, which waits for cutover (P6).
    """
    async with _store(request).lock:
        doc = _store(request).read()
        room = _adaptive_room(doc, room_id)
        what = f"{room['name']}'s adaptive config"
        _check_base(body.base, editor_version(doc, room), what, "the editor")
        proposed = config_mod.checked(
            inventory.set_adaptive_config(doc, room_id, body.adaptive_config)
        )
        after = _room(proposed, room_id)
        names = {b["id"]: b["name"] for b in room.get("bulbs", [])}
        changes = inventory.adaptive_config_changes(
            room["adaptive_config"], after["adaptive_config"], names
        )
        block = after["adaptive_config"]
        summary = (
            f"{len(block['adaptive_bulbs'])} adaptive bulb(s) in "
            f"{len(block['learning_units'])} learning unit(s)"
        )
        result = _commit(
            request,
            doc,
            proposed,
            False,  # an adaptive config binds no hard switch, so makes nothing live
            record=(
                f"Saved the adaptive config of {room['name']}: {summary}"
                + (f", {len(changes)} change(s)." if changes else "; nothing changed.")
            ),
            room_id=room_id,
            detail={"changes": changes},
            category="config",
        )
        return {**result, "changes": changes, "version": editor_version(proposed, after)}


# -- Bulbs -----------------------------------------------------------------------------


def _move_proposal(
    doc: Mapping[str, Any], bulb_id: str, room_id: str | None
) -> tuple[Mapping[str, Any], str | None, dict[str, Any]]:
    room, bulb = inventory.find_bulb(doc, bulb_id)
    if inventory.is_ignored(bulb):
        # Light Map has set the light aside, so it writes nothing about it to HA; its Area
        # is HA's to keep, and a sync follows it.
        raise ApiError(
            409,
            "ignored",
            f"{bulb['name']} is ignored: Light Map doesn't move it. Un-ignore it first, or "
            "move it in Home Assistant.",
        )
    if (room["id"] if room else None) == room_id:
        raise ApiError(409, "no_change", f"bulb '{bulb_id}' is already there")
    area_id = None if room_id is None else _room(doc, room_id)["ha_area_id"]
    return bulb, area_id, inventory.snapshot_with_move(doc, bulb_id, room_id)


@router.post("/bulbs/{bulb_id}/move/preview")
async def move_preview(request: Request, bulb_id: str, body: Move) -> dict[str, Any]:
    """What moving a bulb would change (in HA: its Area; in the snapshot: its room, and the
    memberships it loses), and whether HA would refuse it. Writes nothing."""
    ha = _ha(request)
    doc = _store(request).read()
    bulb, area_id, proposed = _move_proposal(doc, bulb_id, body.room_id)
    return {
        "entity_id": bulb["entity_id"],
        "area_id": area_id,
        "changes": config_mod.snapshot_changes(doc, proposed),
        "problems": _problems(proposed),
        "refused": await inventory.check_move(
            ha, bulb["entity_id"], area_id, match_key=bulb.get("ha_match_key")
        ),
        "fingerprint": fingerprint(proposed),
    }


@router.post("/bulbs/{bulb_id}/move")
async def move(request: Request, bulb_id: str, body: MoveConfirm) -> dict[str, Any]:
    """Move a bulb as previewed: write its Area to HA (verified), then commit the move."""
    ha = _ha(request)
    async with _store(request).lock:
        doc = _store(request).read()
        bulb, area_id, proposed = _move_proposal(doc, bulb_id, body.room_id)
        if fingerprint(proposed) != body.fingerprint:
            raise _stale("The config has changed since the preview")
        config_mod.checked(proposed)  # before the HA write: validate-then-write
        await inventory.move_bulb(
            ha, bulb["entity_id"], area_id, match_key=bulb.get("ha_match_key")
        )
        _write(request, proposed, after_ha=f"{bulb['entity_id']} was moved")
        log.info("moved %s to Area %s", bulb["entity_id"], area_id)
        target = _room(doc, body.room_id)["name"] if body.room_id else inventory.UNASSIGNED
        _record(
            request,
            "map",
            f"Moved {bulb['name']} ({bulb['entity_id']}) to {target}.",
            room_id=body.room_id,
        )
        return {"saved": True, "changes": config_mod.snapshot_changes(doc, proposed)}


@router.put("/bulbs/{bulb_id}/hard_switches")
async def set_bulb_hard_switches(
    request: Request, bulb_id: str, body: HardSwitches
) -> dict[str, Any]:
    """Replace the hard switches bound to a bulb's soft switch."""
    async with _store(request).lock:
        doc = _store(request).read()
        room, bulb = inventory.find_bulb(doc, bulb_id)
        _check_base(body.base, bulb_version(bulb), f"{bulb['name']}'s hard switches")
        proposed = inventory.set_hard_switches(doc, bulb_id, body.hard_switches)
        return _commit(
            request,
            doc,
            proposed,
            body.acknowledge_live,
            record=f"Saved {bulb['name']}'s hard switches ({len(body.hard_switches)}).",
            room_id=room["id"] if room else None,
        )


@router.put("/bulbs/{bulb_id}/ignored")
async def set_ignored(request: Request, bulb_id: str, body: IgnoredFlag) -> dict[str, Any]:
    """Ignore a bulb (a light that isn't a room bulb, such as an indicator LED: from the
    next deploy on it has no soft switch or bridge) or un-ignore it (it comes back bare).

    Ignoring one with hard switches or memberships drops them, so it is refused (409
    `confirm_required`, with what would be dropped) unless `confirm_drops`; the `base`
    (the bulb's `ignore_version`) makes that confirmation cover exactly what the Map
    listed. Pure config: the deploy preview then shows the soft switch and bridge it
    removes.
    """
    async with _store(request).lock:
        doc = _store(request).read()
        room, bulb = inventory.find_bulb(doc, bulb_id)
        if inventory.is_ignored(bulb) == body.ignored:
            # Before the base check, which writes nothing either: a second click, or a
            # second tab asking for the same thing, is told it's done, not that the Map
            # is stale.
            raise ApiError(
                409,
                "no_change",
                f"{bulb['name']} is already {'ignored' if body.ignored else 'not ignored'}",
            )
        _check_base(body.base, ignore_version(doc, bulb), bulb["name"])
        drops = inventory.ignore_drops(doc, bulb_id)
        if body.ignored and drops and not body.confirm_drops:
            raise ApiError(
                409,
                "confirm_required",
                f"Ignoring {bulb['name']} drops {'; '.join(drops)}.",
                drops=drops,
            )
        proposed = inventory.set_ignored(doc, bulb_id, body.ignored)
        changes = config_mod.snapshot_changes(doc, proposed)
        what = f"{bulb['name']} ({bulb['entity_id']})"
        result = _commit(
            request,
            doc,
            proposed,
            False,  # un-ignoring brings a bulb back bare, so nothing is made live
            record=(
                f"Ignored {what}: the next deploy removes its soft switch and bridge."
                if body.ignored
                else f"Un-ignored {what}: the next deploy gives it a soft switch and bridge."
            ),
            room_id=room["id"] if room else None,
            detail={"changes": changes},
        )
        return {**result, "ignored": body.ignored, "changes": changes}


# -- Switching groups ------------------------------------------------------------------


def _group_edit(doc: Mapping[str, Any], room_id: str, group_id: str, body: Group) -> dict[str, Any]:
    group = {"id": group_id, "name": body.name.strip(), "members": body.members}
    proposed = inventory.upsert_switching_group(doc, room_id, group)
    return inventory.set_hard_switches(proposed, group_id, body.hard_switches)


@router.post("/rooms/{room_id}/groups", status_code=201)
async def create_group(request: Request, room_id: str, body: Group) -> dict[str, Any]:
    async with _store(request).lock:
        doc = _store(request).read()
        _room(doc, room_id)
        group_id = inventory.new_group_id(doc, body.name)
        result = _commit(
            request,
            doc,
            _group_edit(doc, room_id, group_id, body),
            body.acknowledge_live,
            record=f"Added the switching group {body.name.strip()}.",
            room_id=room_id,
        )
        return {**result, "group": {"id": group_id}}


@router.put("/rooms/{room_id}/groups/{group_id}")
async def update_group(
    request: Request, room_id: str, group_id: str, body: Group
) -> dict[str, Any]:
    async with _store(request).lock:
        doc = _store(request).read()
        group = _group(_room(doc, room_id), group_id)
        _check_base(body.base, group_version(group), f"Switching group {group['name']}")
        proposed = _group_edit(doc, room_id, group_id, body)
        return _commit(
            request,
            doc,
            proposed,
            body.acknowledge_live,
            record=f"Saved the switching group {body.name.strip()}.",
            room_id=room_id,
        )


@router.delete("/rooms/{room_id}/groups/{group_id}")
async def delete_group(
    request: Request, room_id: str, group_id: str, base: str | None = None
) -> dict[str, Any]:
    """Remove a switching group (`?base=` its version, as shown); its hard switches go
    with it (the next deploy removes their automations)."""
    async with _store(request).lock:
        doc = _store(request).read()
        group = _group(_room(doc, room_id), group_id)
        _check_base(base, group_version(group), f"Switching group {group['name']}")
        proposed = inventory.remove_switching_group(doc, room_id, group_id)
        return _commit(
            request,
            doc,
            proposed,
            False,
            record=f"Removed the switching group {group['name']}.",
            room_id=room_id,
        )


# -- Deploy: the switching plane -------------------------------------------------------


def _plan_digest(deploy_plan: Mapping[str, Any]) -> str:
    return fingerprint({k: deploy_plan[k] for k in ("prefix", "steps", "warnings")})


def _going_live(doc: Mapping[str, Any], deploy_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The hard-switch automations the plan creates or rewrites, with the bulbs each
    drives once it is live."""
    targets = inventory.hard_switch_targets(doc)
    found = []
    for step in deploy_plan["steps"]:
        if step.get("kind") != "automation" or step["op"] not in ("create", "update"):
            continue
        binding_id = step["id"].removeprefix(switching.hard_id(""))
        if step["id"] == switching.hard_id(binding_id) and binding_id in targets:
            target = targets[binding_id]
            found.append(
                {
                    "id": binding_id,
                    "automation": step["id"],
                    "op": step["op"],
                    "name": target["binding"].get("name"),
                    "owner": target["owner"],
                    "bulbs": target["bulbs"],
                }
            )
    return found


async def _module_lanes(ha: HAClient, doc: Mapping[str, Any]) -> dict[str, Any]:
    """Each wired module lane's plan (or why it would be refused), read-only: nothing
    here is applied, and there is no fingerprint to apply it with. Empty while the cutover
    switch is off, since no module lane is registered. A lane HA can't be read for is
    shown as such: it must never cost the switching deploy its preview (and so its
    fingerprint)."""
    lanes: dict[str, Any] = {}
    for prefix in generate_mod.namespaces():
        if prefix == switching.PREFIX:
            continue
        try:
            lane_plan = await deploy.prepare(ha, doc, prefix)
        except (ConfigError, DeployError) as exc:
            lanes[prefix] = {"ready": False, "refused": exc.problems}
        except HAError as exc:
            lanes[prefix] = {"ready": False, "refused": [f"Home Assistant: {exc}"]}
        else:
            lanes[prefix] = {"ready": True, "refused": [], "plan": deploy.summarise(lane_plan)}
    return lanes


@router.get("/deploy/preview")
async def deploy_preview(request: Request) -> dict[str, Any]:
    """Everything a switching-plane deploy would do, writing nothing: the plan for the
    `lm_` lane (or why it would be refused), the hard switches it puts live, the crossover
    report (what else in HA drives these bulbs; names already taken), and the coexistence
    note. `ready` and `fingerprint` say whether, and how, to apply it. With the cutover
    switch on, `modules` also lists each module lane's plan, read-only (P5.1): the key is
    absent while it is off."""
    ha = _ha(request)
    doc = _store(request).read()
    preview: dict[str, Any] = {
        "coexistence": {"note": COEXISTENCE_NOTE, "live_bulbs": sorted(inventory.live_bulbs(doc))}
    }
    try:
        deploy_plan = await deploy.prepare(ha, doc, switching.PREFIX)
    except (ConfigError, DeployError) as exc:
        preview |= {"ready": False, "refused": exc.problems}
    else:
        preview |= {
            "ready": True,
            "refused": [],
            "plan": deploy.summarise(deploy_plan),
            "going_live": _going_live(doc, deploy_plan),
            "fingerprint": _plan_digest(deploy_plan),
        }
    preview["crossover"] = await deploy.crossover(ha, doc)
    lanes = await _module_lanes(ha, doc)
    if lanes:
        preview["modules"] = {"note": MODULE_LANES_NOTE, "lanes": lanes}
    return preview


@router.post("/deploy")
async def deploy_apply(request: Request, body: Confirm) -> dict[str, Any]:
    """Run the switching-plane deploy exactly as previewed, then verify it."""
    ha = _ha(request)
    async with _store(request).lock:
        doc = _store(request).read()
        deploy_plan = await deploy.prepare(ha, doc, switching.PREFIX)
        if _plan_digest(deploy_plan) != body.fingerprint:
            raise _stale("What a deploy would do has changed since the preview")
        try:
            report = await deploy.apply(ha, deploy_plan)
            await deploy.verify(
                ha, deploy_plan, synced=report["synced"], delay=request.app.state.verify_delay
            )
        except (DeployError, HAError) as exc:
            problems = exc.problems if isinstance(exc, DeployError) else [str(exc)]
            _record(
                request,
                "deploy",
                "The switching-plane deploy stopped or did not verify.",
                detail={"problems": problems},
                level="error",
            )
            raise ApiError(
                502,
                "deploy_failed",
                "The deploy stopped or did not verify, so Home Assistant may hold part of it. "
                "Preview and deploy again: the next deploy reconciles from whatever HA holds.",
                problems=problems,
            ) from exc
        log.info("deployed the lm_ lane: %d steps", len(deploy_plan["steps"]))
        result = deploy.summarise(deploy_plan, report)
        counts = ", ".join(
            f"{sum(len(ids) for ids in result[label].values())} {label}"
            for label in ("created", "updated", "removed")
        )
        _record(request, "deploy", f"Deployed the switching plane: {counts}.", detail=result)
        return {"deployed": True, "result": result}


# -- Deploy: Area writes ---------------------------------------------------------------


async def _areas_plan(request: Request) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    ha = _ha(request)
    doc = _store(request).read()
    areas = await ha.list_areas()
    return doc, deploy.plan_areas(doc, areas, await inventory.discover_lights(ha)), areas


@router.get("/areas/preview")
async def areas_preview(request: Request) -> dict[str, Any]:
    """The Area writes that would make HA match the committed snapshot, and anything that
    would refuse them. Writes nothing."""
    ha = _ha(request)
    doc, area_plan, areas = await _areas_plan(request)
    names = {a.get("area_id"): a.get("name") for a in areas}
    keys = deploy.match_keys(doc)
    problems = list(area_plan["problems"])
    for move_ in area_plan["moves"]:
        reason = await inventory.check_move(
            ha, move_["entity_id"], move_["to"], match_key=keys.get(move_["bulb"])
        )
        if reason:
            problems.append(f"bulb '{move_['bulb']}': {reason}")
    return {
        "moves": [
            {
                **move_,
                "from_name": names.get(move_["from"], inventory.UNASSIGNED),
                "to_name": names.get(move_["to"], inventory.UNASSIGNED),
            }
            for move_ in area_plan["moves"]
        ],
        "problems": problems,
        "ready": bool(area_plan["moves"]) and not problems,
        "fingerprint": fingerprint(area_plan["moves"]),
        "note": AREAS_NOTE,
    }


@router.post("/areas/deploy")
async def areas_deploy(request: Request, body: Confirm) -> dict[str, Any]:
    """Write the committed snapshot to HA's Areas exactly as previewed (verified)."""
    ha = _ha(request)
    async with _store(request).lock:
        doc, area_plan, _ = await _areas_plan(request)
        if fingerprint(area_plan["moves"]) != body.fingerprint:
            raise _stale("HA's Areas, or the snapshot, have changed since the preview")
        try:
            result = await deploy.deploy_areas(ha, doc, expected=area_plan["moves"])
        except StalePreview as exc:
            raise _stale("HA's Areas have changed since the preview") from exc
        except DeployError as exc:
            if exc.written is None:
                raise  # refused before writing anything
            _record(
                request,
                "areas",
                f"The Area writes stopped partway: {len(exc.written)} of "
                f"{len(area_plan['moves'])} moves landed.",
                detail={"problems": exc.problems, "written": exc.written},
                level="error",
            )
            raise ApiError(
                502,
                "areas_partial",
                f"The Area writes stopped partway or did not verify: {len(exc.written)} of "
                f"{len(area_plan['moves'])} moves landed. Preview again: it starts from "
                "whatever HA now holds.",
                problems=exc.problems,
                written=exc.written,
            ) from exc
        log.info("wrote %d Area moves to HA", len(result["moves"]))
        _record(
            request,
            "areas",
            f"Wrote {len(result['moves'])} Area move(s) to Home Assistant.",
            detail={"moves": result["moves"]},
        )
        return result


# -- The modules -----------------------------------------------------------------------


def wire_modules(*, adaptive: bool = ADAPTIVE_ENABLED) -> None:
    """Attach the modules to the core's seams. Called once, below, for the live app, with
    the cutover switch (off until P6: nothing is registered, and the core runs exactly as
    in P5.0). `adaptive=True` registers Adaptive Light's `al` namespace (its generator and
    what it addresses in HA) and its bridge brightness; the registries are the process's,
    so a test does it with them patched (`tests.fakes.adaptive_wired`). The module's
    observation seam needs the observer, which exists only in the lifespan: there, a wired
    module (`adaptive_wired_in`) gets its runtime."""
    if adaptive:
        generate_mod.register_namespace(
            adaptive_generate.PREFIX,
            adaptive_generate.generate,
            references=adaptive_generate.references,
        )
        switching.register_bridge_brightness(
            adaptive_generate.PREFIX, adaptive_generate.bridge_brightness
        )


wire_modules()


def adaptive_wired_in() -> bool:
    """Whether the adaptive module is wired in (the cutover switch, or a test's own
    wiring): the one signal the lifespan's runtime and `/api/status` both follow."""
    return adaptive_generate.PREFIX in generate_mod.namespaces()


# -- The app ---------------------------------------------------------------------------


def _error(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail, **extra}, status_code=status)


def _install_errors(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.error, exc.detail, **exc.extra)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(ConfigError)
    async def invalid(_: Request, exc: ConfigError) -> JSONResponse:
        return _error(
            422,
            "invalid_config",
            "Not saved: the config would not validate.",
            problems=exc.problems,
        )

    @app.exception_handler(InventoryError)
    async def refused(_: Request, exc: InventoryError) -> JSONResponse:
        return _error(409, "refused", str(exc))

    @app.exception_handler(DeployError)
    async def deploy_refused(_: Request, exc: DeployError) -> JSONResponse:
        if exc.written is not None:
            # Every caller that writes handles its own partial failure; this is the net.
            return _error(
                502, "deploy_failed", "Stopped after writing part of it.", problems=exc.problems
            )
        return _error(409, "deploy_refused", "Refused before writing.", problems=exc.problems)

    @app.exception_handler(HAAuthError)
    async def ha_auth(_: Request, exc: HAAuthError) -> JSONResponse:
        return _error(502, "ha_auth", f"Home Assistant rejected the token: {exc}")

    @app.exception_handler(HAError)
    async def ha_error(_: Request, exc: HAError) -> JSONResponse:
        return _error(502, "ha_error", f"Home Assistant: {exc}")

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            f"{'/'.join(str(part) for part in e.get('loc', ()))}: {e.get('msg')}"
            for e in exc.errors()
        ]
        return _error(422, "bad_request", "The request was not understood.", problems=problems)


def allowed_hosts(value: str | None) -> tuple[list[str], list[str]]:
    """The Host allowlist from LM_ALLOWED_HOSTS (comma-separated), and the entries it had
    to leave out. Always the loopback names; an entry may be a name, an address, or
    `*.domain`, and a scheme or port pasted with it is dropped, since the Host check
    compares names only. `*` alone turns the check off."""
    hosts, skipped = list(LOOPBACK_HOSTS), []
    for raw in (value or "").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        name = entry.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
        if name.count(":") == 1:
            name = name.split(":", 1)[0]
        wildcard_ok = name == "*" or (name.startswith("*.") and "*" not in name[2:])
        if not name or ("*" in name and not wildcard_ok) or ":" in name or "[" in name:
            skipped.append(raw.strip())
        elif name not in hosts:
            hosts.append(name)
    return hosts, skipped


def create_app(
    *,
    ha: HAClient | None = None,
    config_path: Path | None = None,
    verify_delay: float = deploy.VERIFY_DELAY,
    data_dir: Path | None = None,
    observe: bool | None = None,
    hosts: Sequence[str] | None = None,
) -> FastAPI:
    """Build the app. No I/O: the config is read per request, and the HA client, the store
    and the observer are made in the lifespan. `ha` replaces the client (tests pass a
    fake); `config_path` defaults to LM_CONFIG_PATH, else /data/config.json; `data_dir`
    overrides where the store lives. The observer runs when `observe` says so, by default
    only with the app's own client (a test's fake may not speak HA's event API). `hosts` is
    the Host allowlist, by default from LM_ALLOWED_HOSTS."""
    path = config_path or Path(os.environ.get(CONFIG_PATH_ENV, "").strip() or DEFAULT_CONFIG_PATH)
    if hosts is None:
        hosts, skipped = allowed_hosts(os.environ.get(ALLOWED_HOSTS_ENV))
    else:
        hosts, skipped = list(hosts), []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if skipped:
            log.warning("%s: ignoring what isn't a host name: %s", ALLOWED_HOSTS_ENV, skipped)
        if "*" in hosts:
            log.warning("%s=* turns the Host check off for every site", ALLOWED_HOSTS_ENV)
        # The base URL and the data directory may come from the config; a config that
        # doesn't load still leaves LM_HA_BASE_URL, and the config's own directory.
        try:
            base = app.state.store.read()
        except ApiError:
            base = config_mod.blank()
        owned = None
        if app.state.ha is None:
            try:
                owned = HAClient(**config_mod.connection(base))
                app.state.ha, app.state.ha_error = owned, None
            except (ConfigError, HAError) as exc:
                # Served anyway: the Map still shows the committed config, and every
                # action that needs HA says why it can't reach it.
                app.state.ha_error = str(exc)
                log.warning("Home Assistant is not configured: %s", exc)
        data = Store(
            data_dir or config_mod.data_dir(base, path),
            retention_days=config_mod.system(base)["event_log_retention_days"],
        )
        try:
            data.open()
            app.state.data, app.state.data_error = data, None
        except Exception as exc:  # noqa: BLE001 - whatever it was, the Map still serves
            # Served anyway, without observation or the event log: the Map needs neither,
            # and a damaged archive must never be what keeps it from starting.
            app.state.data_error = f"{data.data_dir}: {exc}"
            log.warning("the store did not open, so nothing is observed: %s", exc)
            data = None
        observer = None
        runtime = None
        if not (ha is None if observe is None else observe):
            app.state.observer_reason = "observation is off for this app"
        elif app.state.ha is None:
            app.state.observer_reason = f"Home Assistant is not configured: {app.state.ha_error}"
        elif data is None:
            app.state.observer_reason = f"the store did not open: {app.state.data_error}"
        else:
            observer = Observer(app.state.ha, data, app.state.store.read)
            app.state.observer = observer
            if adaptive_wired_in():
                # Only with the module wired in (the cutover switch, off until P6): the
                # runtime fires the al_ scenes and publishes the almanac, which are the live
                # ha-adaptive-light's own names. Hooked in before the observer starts, so
                # its first subscription already waits on the adaptive guards.
                runtime = AdaptiveRuntime(app.state.ha, observer, data, app.state.store.read)
                app.state.runtime = runtime
                await runtime.start()
            await observer.start()
        try:
            yield
        finally:
            if runtime is not None:
                app.state.runtime = None
                await runtime.stop()
            if observer is not None:
                app.state.observer = None
                await observer.stop()
            if data is not None:
                app.state.data = None
                data.close()
            if owned is not None:
                app.state.ha = None
                await owned.close()

    # FastAPI's auto-generated /docs, /redoc and /openapi.json are disabled so the HTTP
    # surface is exactly what this file declares, with nothing implicit.
    app = FastAPI(
        title="Light Map", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = ConfigStore(path)
    app.state.ha = ha
    app.state.ha_error = None if ha is not None else "not connected yet"
    app.state.verify_delay = verify_delay
    app.state.data = None  # the store: the archive and its query layer
    app.state.data_error = "not opened yet"
    app.state.observer = None
    app.state.observer_reason = "not started yet"
    app.state.runtime = None  # the adaptive runtime, only with the module wired in
    app.state.allowed_hosts = hosts
    # Outermost, so a request for an unknown Host reaches nothing, static files included.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)
    _install_errors(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness only: says the process is serving, nothing about Home Assistant."""
        return {"status": "ok"}

    app.include_router(router)
    # Mounted last: a mount at "/" matches every path, so any route declared after it would
    # be shadowed by the static files.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
