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
registers itself; no module is attached yet (Adaptive Light arrives in P5).

The lifespan also opens the store (the CSV archive and its query layer, beside the config
unless `system.data_dir` says otherwise) and starts the observer (P4), which subscribes to
HA's events once the config has a room with bulbs. Every config save tells the observer to
re-read the config. Without HA, or with a data directory that can't be opened, the Map
still serves, and `/api/status` says what is missing.

The HTTP surface is the Map tab's (P3) and the Log tab's (P4). Everything under /api/
answers JSON; an error is `{error, detail, ...}` with `problems` when there are several.
Every request first passes the Host allowlist (`LM_ALLOWED_HOSTS`, plus `localhost` and
`127.0.0.1`), which refuses a DNS-rebinding page; it is not authentication.

- Reads: `GET /api/status`, `/api/map` (the Map's view of the committed config),
  `/api/config` (the config itself, token-free), `/api/entities` (HA entities a hard
  switch can take as its source), `/api/log` (the event log, newest first, by minimum
  level, category and room).
- Config edits, each validate-then-write: `PUT /api/rooms/{room}/adaptive`,
  `PUT /api/bulbs/{bulb}/hard_switches`, `POST /api/rooms/{room}/groups`,
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
  snapshot back to HA's Areas). So nothing is ever applied that wasn't shown first.

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
from app.core.config import ConfigError
from app.core.deploy import DeployError, StalePreview
from app.core.ha import HAAuthError, HAClient, HAError
from app.core.inventory import InventoryError, NotFound
from app.core.observe import Observer
from app.core.store import LEVELS, Store

log = logging.getLogger(__name__)

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

COEXISTENCE_WARNING = (
    "Binding a hard switch makes its bulbs live: once deployed, pressing it drives them "
    "through Light Map's bridges. Until cutover (P6) Light Map runs beside "
    "ha-adaptive-light, which keeps driving the same bulbs, and neither knows what the "
    "other did. Bind only a switch the old system doesn't use. Send acknowledge_live to "
    "save it anyway."
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


def _check_base(seen: str | None, current: str, what: str) -> None:
    """Refuse an edit made against a `version` other than the current one: another tab, or
    a sync, changed the thing after the Map showed it. Edits replace whole lists (a bulb's
    hard switches, a group's members), so saving the stale view would silently undo the
    other change, and an acknowledgement or confirmation given for the old view would
    cover what the user never saw."""
    if seen != current:
        raise ApiError(
            409,
            "edit_stale",
            f"{what} has changed since the Map showed it (in another tab, or by a sync): "
            "reload the Map and make the edit again.",
        )


# The `version` the Map shows for each editable thing, which an edit sends back as `base`.


def bulb_version(bulb: Mapping[str, Any]) -> str:
    return fingerprint(bulb.get("hard_switches", []))


def group_version(group: Mapping[str, Any]) -> str:
    return fingerprint({k: group.get(k) for k in ("name", "members", "hard_switches")})


def room_version(room: Mapping[str, Any]) -> str:
    return fingerprint({"adaptive": room.get("adaptive"), "config": room.get("adaptive_config")})


def _commit(
    request: Request,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    acknowledge: bool,
    *,
    record: str,
    room_id: str | None = None,
) -> dict[str, Any]:
    """Validate a config edit, refuse it while it makes a hard switch drive something new
    unless that is acknowledged, then save it and note it (`record`) in the event log.
    Returns what it made live."""
    checked = config_mod.checked(after)
    live = inventory.going_live(before, checked)
    if live and not acknowledge:
        raise ApiError(409, "coexistence", COEXISTENCE_WARNING, going_live=live)
    _write(request, checked)
    if live:
        record += " It makes a hard switch drive something new (acknowledged)."
    _record(request, "map", record, room_id=room_id, detail={"going_live": live} if live else None)
    return {"saved": True, "going_live": live}


# -- Views -----------------------------------------------------------------------------


def map_view(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The Map's view of the committed config: rooms by name, then Unassigned; each bulb
    with its soft switch and whether it is live (a hard switch drives it, directly or
    through a switching group); each room's groups."""
    live = inventory.live_bulbs(doc)
    targets = inventory.hard_switch_targets(doc)

    def bulb_view(bulb: Mapping[str, Any], groups_of: Mapping[str, list[str]]) -> dict[str, Any]:
        bulb_id = bulb["id"]
        return {
            "id": bulb_id,
            "name": bulb["name"],
            "entity_id": bulb["entity_id"],
            "version": bulb_version(bulb),
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

    rooms = []
    for room in sorted(doc.get("rooms", []), key=lambda r: (r["name"].casefold(), r["id"])):
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
                "bulbs": [bulb_view(b, groups_of) for b in room.get("bulbs", [])],
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
    return {
        "rooms": rooms,
        "unassigned": {
            "name": inventory.UNASSIGNED,
            "bulbs": [bulb_view(b, {}) for b in doc.get("unassigned", [])],
        },
        "live": {"bulbs": sorted(live), "hard_switches": len(targets)},
    }


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
    marks a bulb Light Map drives (a toggle may not take one: it would toggle itself)."""
    ha = _ha(request)
    doc = _store(request).read()
    mapped = set(switching.references(doc)["bulbs"])
    found = []
    for state in await ha.get_states():
        entity_id = str(state.get("entity_id", ""))
        domain, _, object_id = entity_id.partition(".")
        if domain not in SOURCE_DOMAINS or object_id.startswith("lm_"):
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
    """Flag a room adaptive (it gets a minimal adaptive_config; the editor arrives in P5)
    or not. Unflagging a room whose adaptive_config holds anything is refused (409
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


# -- Bulbs -----------------------------------------------------------------------------


def _move_proposal(
    doc: Mapping[str, Any], bulb_id: str, room_id: str | None
) -> tuple[Mapping[str, Any], str | None, dict[str, Any]]:
    room, bulb = inventory.find_bulb(doc, bulb_id)
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


@router.get("/deploy/preview")
async def deploy_preview(request: Request) -> dict[str, Any]:
    """Everything a switching-plane deploy would do, writing nothing: the plan for the
    `lm_` lane (or why it would be refused), the hard switches it puts live, the crossover
    report (what else in HA drives these bulbs; names already taken), and the coexistence
    note. `ready` and `fingerprint` say whether, and how, to apply it."""
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
        if not (ha is None if observe is None else observe):
            app.state.observer_reason = "observation is off for this app"
        elif app.state.ha is None:
            app.state.observer_reason = f"Home Assistant is not configured: {app.state.ha_error}"
        elif data is None:
            app.state.observer_reason = f"the store did not open: {app.state.data_error}"
        else:
            observer = Observer(app.state.ha, data, app.state.store.read)
            app.state.observer = observer
            await observer.start()
        try:
            yield
        finally:
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
