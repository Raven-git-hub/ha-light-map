"""Adaptive runtime: the adaptive loop: subscribe to core observation, run the scheduler,
fire crossovers, push the almanac.

Thinner than the predecessor's runtime, which also did the observing: `core.observe` takes
the heartbeats and detects the reactive events, and this plugs into it through its seams
(docs/DESIGN.md, "The runtime"):

- `register_section`: the section each adaptive room is in, recorded with every
  observation (the one this last fired for the room, the scene select's; before its first
  crossover, the scheduled one);
- `register_guard`: `input_boolean.al_active_<room>`, raised by the room's own scene and
  maintenance runs, so a heartbeat waits them out and their late transition steps are
  never learned as a person at a dimmer;
- `subscribe`: a reactive event in an adaptive room raises `input_boolean.al_hold_<room>`,
  so maintenance stands down for the rest of the section (the next crossover's scene
  lowers it).

Three things it does on its own clock, every `TICK_SECONDS`:

- **Crossovers, level-triggered.** Rather than setting a timer for each boundary, it asks
  which section each adaptive room should be in now (`scheduler.active_section_at`, from the
  room's schedule profile and HA's location) and fires a crossover whenever that differs
  from the one it fired: the room's scene select is set to the section and its
  `al_scene_<room>_<section>` fired with `automation.trigger` (which skips conditions:
  that is why scenes carry none). Normal running, a start after downtime, a dropped
  connection and a clock change all take that one path. At its first tick it adopts the
  section each room's select already shows, so a restart doesn't fire the current scene
  again, which would turn back on the soft switches the user has turned off since.
- **The nightly pass**, at `ANALYSIS_AT` local time (or at the first tick after, when it was
  down then; a pass the store already holds for the day counts): `analyser.run` in a
  worker thread, its snapshots recorded (`almanac.record`).
- **The almanac's publish, level-triggered too**: each tick it builds every adaptive room's
  payload (`almanac.latest` with the config as it now is) and publishes it when it differs
  from what it last published. A unit turned off in the editor, a snapshot coming into
  force at midnight, a new pass: each is a republish, and nothing else is. A state set over
  REST doesn't survive an HA restart, so a reconnect forgets what was published.

What it reads of HA itself: the location (`/api/config`), and, at its start, the scene
selects (to adopt the section in force) and the automations (to find each scene by its `id`
attribute rather than assume its entity id: one adopted by an update keeps its old one).
Never a bulb or a sensor: observation is the observer's.

Hard rules:

- Observation comes only from the `core.observe` subscription; the loop never scrapes HA.
  It never records an observation or reclassifies one (core did both).
- Actuation is only by commanding soft switches (power) and setting brightness guarded on
  soft-on, never by powering bulbs directly: it sets the scene select, fires scene
  automations (which command soft switches), raises the hold and publishes the almanac.
- Scene automations are fired via `automation.trigger`, which skips conditions; that is why
  they carry `conditions: []`.
- It runs only when the composition root has wired the adaptive module in (the cutover
  switch, `app.main.ADAPTIVE_ENABLED`, off until P6): the `al_` names are the live
  ha-adaptive-light's own.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time, timezone
from typing import Any

from app.core.config import SECTIONS
from app.core.ha import HAClient, HAError
from app.core.observe import Observer
from app.core.store import Store, timestamp
from app.modules.adaptive import almanac, analyser, scheduler
from app.modules.adaptive import generate as al

log = logging.getLogger(__name__)

# How often the loop asks which section each room should be in (the predecessor's): a
# crossover fires at most this late.
TICK_SECONDS = 20.0
# When the nightly pass runs, local time: after midnight, so the day before is complete.
ANALYSIS_AT = time(0, 15)
# A crossover later than this is logged as a catch-up.
LATE_SECONDS = 120
# How long to wait for HA to answer automation.trigger past the run's own length (its
# transition and settle): HA answers only when the run is over.
TRIGGER_MARGIN_SECONDS = 60
# The least time between two re-reads of HA's automations when a scene isn't found.
REMAP_SECONDS = 60.0

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


class AdaptiveRuntime:
    """The Adaptive Light loop for every room flagged adaptive.

    `config` is the config, or a function returning the current one (re-read every tick; a
    config that fails to load leaves the last one that did). `now` is the clock, for tests;
    `tick_seconds` the loop's period.
    """

    def __init__(
        self,
        ha: HAClient,
        observer: Observer,
        store: Store,
        config: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
        *,
        now: Callable[[], datetime] | None = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.ha = ha
        self.observer = observer
        self.store = store
        self._load = config if callable(config) else (lambda: config)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tick_seconds = tick_seconds
        self._config: Mapping[str, Any] = {}
        self._config_problem: str | None = None
        self._provider: scheduler.AstralSunProvider | None = None
        # The section fired (or adopted from its select) per room, and the rooms whose select
        # has been read.
        self._fired: dict[str, str] = {}
        self._adopted: set[str] = set()
        # Scene automation id -> entity id, from HA's automation entities' `id` attribute.
        self._scenes: dict[str, str] = {}
        self._mapped_at: float | None = None
        # Per room, the payload last published (canonical JSON).
        self._published: dict[str, str] = {}
        self._analysed_on: date | None = None
        self._collapses_logged: dict[str, date] = {}
        # What has been logged as failing (key -> message), so a failure is logged once.
        self._failing: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._started = False
        self.last_analysis: str | None = None

    # -- Lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Hook into the observer (the section, the guard, the observations) and start the
        loop in the background; returns at once."""
        if self._started:
            return
        self._attach()
        self._task = asyncio.create_task(self._run(), name="adaptive-runtime")

    def _attach(self) -> None:
        """The hooks alone, without the loop (for tests, which tick by hand)."""
        self._started = True
        self._reload()
        self.observer.register_section(self.section)
        self.observer.register_guard(self.guards)
        self.observer.subscribe(self.on_observation)
        self.ha.add_connection_listener(self._on_connection)

    async def stop(self) -> None:
        """Stop the loop. A scene already fired runs on in HA."""
        self.ha.remove_connection_listener(self._on_connection)
        tasks = [t for t in (self._task, *self._background) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick(self._now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any one tick
                log.exception("an adaptive tick failed")
                self._fail("tick", "error", f"An adaptive runtime tick failed: {exc!r}")
            await asyncio.sleep(self._tick_seconds)

    def status(self) -> dict[str, Any]:
        """For the status line: each adaptive room's section, the last nightly pass, and
        which rooms' almanacs are published."""
        return {
            "running": self._task is not None and not self._task.done(),
            "located": self._provider is not None,
            "sections": dict(sorted(self._fired.items())),
            "last_analysis": self.last_analysis,
            "published": sorted(self._published),
            "failing": sorted(set(self._failing.values())),
        }

    # -- The observer's hooks ----------------------------------------------------------

    def section(self, room_id: str) -> str | None:
        """The section a room is in, for the observer: the one fired for it, or before its
        first crossover the scheduled one; None for a room that isn't adaptive."""
        room = self._room(room_id)
        if room is None:
            return None
        if room_id in self._fired:
            return self._fired[room_id]
        if self._provider is None:
            return None
        try:
            local = self._now().astimezone(self._provider.tzinfo)
            return scheduler.active_section_at(
                self._profile(self._config, room), self._provider, local, self._provider.tzinfo
            )[0]
        except Exception:  # noqa: BLE001 - no section is recorded rather than none observed
            return None

    def guards(self, config: Mapping[str, Any], room: Mapping[str, Any]) -> list[str]:
        """The guard the observer waits on for a room: its `al_active_` while adaptive."""
        if room.get("adaptive") is True and isinstance(room.get("adaptive_config"), Mapping):
            return [al.guard_entity(room["id"])]
        return []

    async def on_observation(self, observation: dict[str, Any]) -> None:
        """Handle one heartbeat or reactive event from the platform observer: a reactive
        event in an adaptive room raises its hold. Core recorded it; nothing here does."""
        if observation.get("kind") != "reactive":
            return
        room_id = observation.get("room_id")
        room = self._room(room_id) if isinstance(room_id, str) else None
        if room is None:
            return
        try:
            await self.ha.call_service(
                "input_boolean", "turn_on", {"entity_id": al.hold_entity(room["id"])}
            )
        except HAError as exc:
            self._log(
                "warning",
                "hold",
                f"Could not hold maintenance off in {room['name']}: {exc}",
                room["id"],
            )
            return
        self._log(
            "info",
            "hold",
            f"Maintenance held off in {room['name']} for the rest of the section: the user "
            "changed a light.",
            room["id"],
        )

    async def _on_connection(self, state: str, detail: str | None) -> None:
        if state == "connected":
            # HA may have restarted: what was published over REST is gone, and its location
            # may have changed. Both are fetched again by the next tick.
            self._published.clear()
            self._provider = None

    # -- The loop ----------------------------------------------------------------------

    async def tick(self, now: datetime) -> None:
        """Evaluate each adaptive room's section at `now` and fire any crossover due, run the
        nightly pass when it is due, and publish any almanac that changed."""
        config = self._reload()
        rooms = al.adaptive_rooms(config)
        if not rooms or not await self._locate():
            return
        assert self._provider is not None
        local = now.astimezone(self._provider.tzinfo)
        await self._adopt(rooms)
        for room in rooms:
            if room["id"] in self._adopted:
                await self._schedule(config, room, local)
        await self._nightly(config, local)
        await self._publish_due(rooms, local.date())

    def _reload(self) -> Mapping[str, Any]:
        try:
            self._config = self._load()
            self._config_problem = None
        except Exception as exc:  # noqa: BLE001 - whatever the loader raises, keep going
            problem = str(exc) or type(exc).__name__
            if problem != self._config_problem:
                self._config_problem = problem
                self._log(
                    "warning",
                    "runtime",
                    f"The config did not load, so the adaptive runtime carries on with the last "
                    f"one that did: {problem}",
                )
        return self._config

    async def _locate(self) -> bool:
        """HA's location for the sun (its time zone too), once per connection."""
        if self._provider is not None:
            return True
        try:
            self._provider = scheduler.provider_for(await self.ha.get_config())
        except HAError as exc:
            self._fail("location", "warning", f"Can't read Home Assistant's location yet: {exc}")
            return False
        except ValueError as exc:
            self._fail("location", "error", f"No sections can be scheduled: {exc}")
            return False
        self._ok("location")
        return True

    async def _adopt(self, rooms: Sequence[Mapping[str, Any]]) -> None:
        """For each room not yet seen, the section its select already shows; and the scene
        automations' entity ids."""
        pending = [room for room in rooms if room["id"] not in self._adopted]
        if not pending:
            return
        try:
            states = await self._read_states()
        except HAError as exc:
            self._fail("adopt", "warning", f"Can't read Home Assistant's states yet: {exc}")
            return
        self._ok("adopt")
        for room in pending:
            select = states.get(al.scene_select_entity(room["id"])) or {}
            if select.get("state") in SECTIONS:
                self._fired[room["id"]] = select["state"]
            self._adopted.add(room["id"])

    async def _read_states(self) -> dict[str, Mapping[str, Any]]:
        states = {
            s["entity_id"]: s
            for s in await self.ha.get_states()
            if isinstance(s, Mapping) and isinstance(s.get("entity_id"), str)
        }
        self._scenes = {
            str(state["attributes"]["id"]): entity_id
            for entity_id, state in states.items()
            if entity_id.startswith("automation.")
            and isinstance(state.get("attributes"), Mapping)
            and state["attributes"].get("id")
        }
        self._mapped_at = asyncio.get_running_loop().time()
        return states

    async def _schedule(
        self, config: Mapping[str, Any], room: Mapping[str, Any], local: datetime
    ) -> None:
        room_id = room["id"]
        profile = self._profile(config, room)
        assert self._provider is not None
        try:
            section, started = scheduler.active_section_at(
                profile, self._provider, local, self._provider.tzinfo
            )
        except scheduler.SunUnavailable as exc:
            self._fail(
                f"section:{room_id}",
                "warning",
                f"{room['name']}: no section is in force ({exc}).",
                room_id,
            )
            return
        self._ok(f"section:{room_id}")
        self._log_collapses(room, profile, local.date())
        if section != self._fired.get(room_id):
            await self._crossover(room, section, started, local)

    def _log_collapses(
        self, room: Mapping[str, Any], profile: Mapping[str, Any], day: date
    ) -> None:
        """Once a day per room: the sections the schedule skips today, and why."""
        if self._collapses_logged.get(room["id"]) == day:
            return
        self._collapses_logged[room["id"]] = day
        assert self._provider is not None
        for boundary in scheduler.compute_day(profile, self._provider, day, self._provider.tzinfo):
            if not boundary.ran:
                self._log(
                    "info",
                    "scene",
                    f"{room['name']}: {boundary.section} is skipped today: {boundary.reason}.",
                    room["id"],
                )

    async def _crossover(
        self, room: Mapping[str, Any], section: str, started: datetime, local: datetime
    ) -> None:
        """Move a room into a section: set its scene select and fire its scene. Retried at
        the next tick if HA can't take it."""
        room_id = room["id"]
        scene = al.scene_id(room_id, section)
        entity = self._scenes.get(scene)
        if entity is None and self._remap_due():
            try:
                await self._read_states()
            except HAError:
                pass  # the select below says whether HA can be reached at all
            entity = self._scenes.get(scene)
        try:
            # First, so the bridges and maintenance read the new section even if the scene
            # can't be run; the scene sets it again, which changes nothing.
            await self.ha.call_service(
                "input_select",
                "select_option",
                {"entity_id": al.scene_select_entity(room_id), "option": section},
            )
        except HAError as exc:
            self._fail(
                f"crossover:{room_id}",
                "error",
                f"{room['name']}: couldn't move into {section}; retrying: {exc}",
                room_id,
            )
            return
        self._ok(f"crossover:{room_id}")
        self._fired[room_id] = section
        late = (local - started).total_seconds()
        self._log(
            "info",
            "scene",
            f"{room['name']}: {section} began"
            + (f" ({late:.0f} s after its boundary)" if late > LATE_SECONDS else "")
            + "."
            + ("" if entity else f" Its scene automation {scene} isn't in Home Assistant."),
            room_id,
            {"section": section, "planned": started.isoformat(), "scene": entity},
        )
        if entity is not None:
            transition = int(room["adaptive_config"]["scenes"][section]["transition_seconds"])
            timeout = transition + al.SETTLE_SECONDS + TRIGGER_MARGIN_SECONDS
            self._spawn(self._trigger(room, section, entity, timeout))

    async def _trigger(
        self, room: Mapping[str, Any], section: str, entity: str, timeout: float
    ) -> None:
        """Fire a scene. HA answers only when its run is over, so this runs in the
        background; if HA didn't take it, the crossover is retried at the next tick."""
        try:
            await self.ha.call_service(
                "automation", "trigger", {"entity_id": entity}, timeout=timeout
            )
        except HAError as exc:
            self._log(
                "error",
                "scene",
                f"{room['name']}: the {section} scene ({entity}) failed; retrying: {exc}",
                room["id"],
            )
            if self._fired.get(room["id"]) == section:
                del self._fired[room["id"]]

    def _remap_due(self) -> bool:
        now = asyncio.get_running_loop().time()
        return self._mapped_at is None or now - self._mapped_at >= REMAP_SECONDS

    async def _nightly(self, config: Mapping[str, Any], local: datetime) -> None:
        today = local.date()
        if self._analysed_on == today or local.time() < ANALYSIS_AT:
            return
        if self._analysed_on is None and self._pass_recorded(local):
            self._analysed_on = today
            return
        self._analysed_on = today  # once a day, whatever happens: a failure waits a day
        try:
            rows = await asyncio.to_thread(analyser.run, self.store, config, now=local)
            almanac.record(self.store, rows)
        except Exception as exc:  # noqa: BLE001 - logged; tomorrow's pass tries again
            log.exception("the nightly analysis failed")
            self._log("error", "analysis", f"The nightly analysis failed: {exc!r}")
            return
        self.last_analysis = timestamp(local)
        rooms = sorted({row["room_id"] for row in rows})
        self._log(
            "info",
            "analysis",
            f"Nightly analysis: {len(rows)} snapshot row(s) for {len(rooms)} room(s).",
            detail={
                room_id: {
                    "mode": next(r["mode"] for r in rows if r["room_id"] == room_id),
                    "valid_from": next(r["valid_from"] for r in rows if r["room_id"] == room_id),
                }
                for room_id in rooms
            },
        )

    def _pass_recorded(self, local: datetime) -> bool:
        """Whether the store holds a pass run on this local day (a restart after it ran)."""
        midnight = datetime.combine(local.date(), time(0, 0), tzinfo=local.tzinfo)
        return bool(
            self.store.query("SELECT 1 FROM almanac WHERE ts >= ? LIMIT 1", (timestamp(midnight),))
        )

    async def _publish_due(self, rooms: Sequence[Mapping[str, Any]], today: date) -> None:
        for room in rooms:
            try:
                await self._publish(room, today)
            except HAError as exc:
                self._fail(
                    f"publish:{room['id']}",
                    "warning",
                    f"{room['name']}: couldn't publish the almanac yet: {exc}",
                    room["id"],
                )

    async def _publish(self, room: Mapping[str, Any], today: date, *, force: bool = False) -> bool:
        room_id = room["id"]
        published = almanac.payload(room, almanac.latest(self.store, room_id, today=today))
        digest = json.dumps(published, sort_keys=True)
        if not force and self._published.get(room_id) == digest:
            return False
        await almanac.publish(self.ha, room_id, published)
        self._published[room_id] = digest
        self._ok(f"publish:{room_id}")
        self._log(
            "debug",
            "almanac",
            f"{room['name']}: published its almanac (valid from {published['state']}).",
            room_id,
        )
        return True

    async def publish_almanac(self, room_id: str) -> None:
        """Push a room's current almanac to HA now."""
        room = self._room(room_id)
        if room is None:
            raise KeyError(f"room '{room_id}' is not adaptive")
        if not await self._locate():
            raise HAError("Home Assistant's location (and time zone) can't be read")
        assert self._provider is not None
        await self._publish(room, self._now().astimezone(self._provider.tzinfo).date(), force=True)

    # -- Helpers -----------------------------------------------------------------------

    def _room(self, room_id: str) -> Mapping[str, Any] | None:
        return next((r for r in al.adaptive_rooms(self._config) if r["id"] == room_id), None)

    @staticmethod
    def _profile(config: Mapping[str, Any], room: Mapping[str, Any]) -> Mapping[str, Any]:
        wanted = room["adaptive_config"]["schedule_profile"]
        return next(p for p in config["schedule_profiles"] if p["id"] == wanted)

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _fail(self, key: str, level: str, message: str, room_id: str | None = None) -> None:
        """Log a failure once until it clears (`_ok`): a loop retrying every tick would
        otherwise fill the event log."""
        if self._failing.get(key) == message:
            return
        self._failing[key] = message
        self._log(level, "runtime" if room_id is None else "scene", message, room_id)

    def _ok(self, key: str) -> None:
        self._failing.pop(key, None)

    def _log(
        self,
        level: str,
        category: str,
        message: str,
        room_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Into the event log (and the container's log, from info up). Never raises: a log
        entry that can't be written must not stop the loop."""
        log.log(_LEVELS.get(level, logging.ERROR), "%s", message)
        try:
            self.store.log_event(level, category, message, room_id=room_id, detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("could not write to the event log")
