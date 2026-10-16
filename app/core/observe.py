"""Observation: the live observer: per-bulb heartbeats, reactive (manual-change) detection,
the event log. A platform service modules subscribe to. Readings are per bulb, long format.

The observer watches the bulbs of every room that has any, and those rooms' lux and
presence sensors, through one `state_changed` subscription (`HAClient.subscribe_events`,
kept alive across drops by the client's reconnect ladder). It keeps a mirror of the states
it watches, refreshed from HA's states at every (re)connect and every tick, and:

- every `system.heartbeat_interval_minutes` (aligned to the clock), records per room one
  heartbeat (lux, occupancy, the section a module reports) plus one reading per bulb;
- turns a burst of manual brightness changes in a room, consolidated over
  `system.reactive_window_seconds`, into one reactive event;
- writes both to the store first, then hands them to every subscribed module handler, and
  records what it did and saw in the event log.

It collects nothing module-specific itself. Two hooks let a module put what it knows into
an observation: `register_section` (the section a room is in, recorded with each
observation; adaptive's scheduler at P5) and `register_guard` (entities a module raises
while its own automations change a room's lights, `al_active_<room>` at P5). Nothing
registers either before P5. A room's lux and presence sensors are read from its
`adaptive_config`, where the config shape keeps them, so only adaptive rooms record lux and
occupancy (an open decision in docs/ARCHITECTURE.md).

Hard rules:

- Reactive detection ignores the system's own changes. A brightness change counts as a user
  intervention only when it carries no `context.parent_id` and no guard is raised.
  Switch-driven and bridge-driven changes carry a `parent_id`, so toggling power is never
  learned as a brightness preference. A manual dimmer change on the bulb has no `parent_id`
  and is still learned.
- Observation is per bulb, long format. Pooling into learning units happens at analysis
  time, never here, so learning units can be redrawn without losing history.
- Modules consume observation by subscribing here; they never scrape HA themselves.
- The WebSocket read loop must start before subscribing (see `core.ha`).
- A lux reading of 0 is a floor, not a measurement.
- Never sample mid-transition: a heartbeat waits while a guard on its room is raised.
- The observer only reads HA. It never calls a service or writes a state.
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core import config as config_mod
from app.core.ha import HAClient, HAError
from app.core.store import Store, timestamp

log = logging.getLogger(__name__)

ObservationHandler = Callable[[dict[str, Any]], Awaitable[None]]
# (config, room) -> the entities that, while on, say a module is changing the room's lights.
GuardProvider = Callable[[Mapping[str, Any], Mapping[str, Any]], Iterable[str]]
# room id -> the section the room is in now, or None.
SectionProvider = Callable[[str], str | None]

# How long a heartbeat waits for a raised guard before it is skipped. The predecessor's
# transitions run up to 30 s; a guard up for longer than this is more likely stuck.
GUARD_WAIT_SECONDS = 90.0

# (is_on, brightness): is_on is None when HA has no usable state for the bulb.
Reading = tuple[bool | None, int | None]

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


def is_user_intervention(event: Mapping[str, Any], *, guarded: bool) -> bool:
    """Return True only for a brightness change with no `context.parent_id` while no guard
    is raised.

    A brightness change is a light that was on and is still on, at a different brightness.
    A power change (off->on, on->off) is never one: through the switching plane it carries
    a `parent_id` (the bridge and the hard switches are automations), and out of band it is
    a power decision, not a brightness preference.
    """
    if guarded:
        return False
    data = event.get("data") or {}
    old, new = data.get("old_state"), data.get("new_state")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return False
    if old.get("state") != "on" or new.get("state") != "on":
        return False
    before, after = _brightness(old), _brightness(new)
    if before is None or after is None or before == after:
        return False
    # HA threads a parent_id through every change an automation or script causes, and the
    # event carries the same context as its new state; either one marks it as ours.
    return not (_parent_id(new) or _parent_id(event))


@dataclass(frozen=True)
class _Room:
    id: str
    name: str
    bulbs: tuple[tuple[str, str], ...]  # (bulb id, entity id), by bulb id
    lux_sensors: tuple[str, ...]
    presence_sensors: tuple[str, ...]
    guards: tuple[str, ...]


@dataclass
class _Window:
    """A room's open reactive window: one user intervention in the making."""

    opened: datetime
    seconds: int
    section: str | None
    lux_before: float | None
    entities: dict[str, str]  # bulb id -> entity id
    before: dict[str, Reading]
    touched: set[str] = field(default_factory=set)
    timer: asyncio.Task[None] | None = None

    @property
    def deadline(self) -> datetime:
        return self.opened + timedelta(seconds=self.seconds)


class Observer:
    """The platform observation service: one HA event subscription, fanned out to modules.

    `config` is the config, or a function returning the current one; it is re-read at
    every tick and on `reload`, and a config that fails to load leaves the last one that
    did. `now` is the clock and `guard_wait` the guard deferral limit, for tests.
    """

    def __init__(
        self,
        ha: HAClient,
        store: Store,
        config: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
        *,
        now: Callable[[], datetime] | None = None,
        guard_wait: float = GUARD_WAIT_SECONDS,
    ) -> None:
        self.ha = ha
        self.store = store
        self._load = config if callable(config) else (lambda: config)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._guard_wait = guard_wait
        self._handlers: list[ObservationHandler] = []
        self._guard_providers: list[GuardProvider] = []
        self._section_provider: SectionProvider | None = None
        self._config: Mapping[str, Any] = {}
        self._config_problem: str | None = None
        self._settings = config_mod.system({})
        self._rooms: dict[str, _Room] = {}
        self._bulbs: dict[str, tuple[str, str]] = {}  # entity id -> (room id, bulb id)
        self._guard_entities: set[str] = set()
        self._watched: set[str] = set()
        self._states: dict[str, Mapping[str, Any]] = {}  # the mirror
        self._windows: dict[str, _Window] = {}
        self._guard_signal = asyncio.Event()
        self._refreshing: asyncio.Future[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._subscribed = False
        self._stopping = False
        self._ever_connected = False
        self._unreachable = False
        self._pruned_on: date | None = None
        self.connected = False
        self.events_seen = 0
        self.last_event_at: str | None = None
        self.last_heartbeat_at: str | None = None

    # -- Module hooks ------------------------------------------------------------------

    def subscribe(self, handler: ObservationHandler) -> None:
        """Register a module's handler for heartbeats and reactive events.

        Each observation is `{"kind": "heartbeat", "room_id", "heartbeat", "readings"}` or
        `{"kind": "reactive", "room_id", "event", "changes"}`, with the rows as the store
        recorded them. It is handed over after the store has it; a handler that fails is
        logged, and stops neither the others nor the observer.
        """
        if handler not in self._handlers:
            self._handlers.append(handler)

    def register_guard(self, provider: GuardProvider) -> None:
        """Let a module name, per room, the entities it raises while its own automations
        change the room's lights. While any is on, the room's brightness changes are never
        learned, and its heartbeat waits.

        A guard covers what `parent_id` can't: HA keeps a service call's context on an
        entity's state for only five seconds, so the later steps of a longer transition
        arrive with no `parent_id` and would read as a person at a dimmer.
        """
        self._guard_providers.append(provider)
        self._apply(self._config)

    def register_section(self, provider: SectionProvider) -> None:
        """Let the module that has sections say which one a room is in; it is recorded with
        each observation, never inferred later from the clock. At most one."""
        if self._section_provider is not None:
            raise ValueError("a section provider is already registered")
        self._section_provider = provider

    # -- Lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Start observing in the background; returns at once. The subscription opens the
        WebSocket (its read loop runs before the subscribe command: `core.ha`) once the
        config has a room with bulbs, and HA's client keeps it alive from then on."""
        if self._task is not None:
            return
        self._stopping = False
        self.reload()
        self.ha.add_connection_listener(self._on_connection)
        self._task = asyncio.create_task(self._run(), name="observer")

    async def stop(self) -> None:
        """Stop observing and flush anything pending to the store."""
        self._stopping = True
        tasks = [t for t in (self._task, *self._background) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.flush(everything=True)
        if self._subscribed:
            self._subscribed = False
            with suppress(HAError):
                await self.ha.unsubscribe_events("state_changed", self._on_event)
        self.ha.remove_connection_listener(self._on_connection)
        self.connected = False

    def reload(self) -> bool:
        """Re-read the config now; False if it didn't load (the last good one stays)."""
        try:
            config = self._load()
        except Exception as exc:  # noqa: BLE001 - whatever the loader raises, keep going
            problem = str(exc) or type(exc).__name__
            if problem != self._config_problem:
                self._config_problem = problem
                self._log(
                    "warning",
                    "observer",
                    f"The config did not load, so observation carries on with the last one "
                    f"that did: {problem}",
                )
            return False
        self._config_problem = None
        self._apply(config)
        return True

    def status(self) -> dict[str, Any]:
        """For the status line: is it running, and is anything arriving."""
        return {
            "running": self._task is not None and not self._task.done(),
            "subscribed": self._subscribed,
            "connected": self.connected,
            "rooms": len(self._rooms),
            "bulbs": len(self._bulbs),
            "events_seen": self.events_seen,
            "last_event_at": self.last_event_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "open_windows": len(self._windows),
            "config_problem": self._config_problem,
        }

    def guarded(self, room_id: str) -> bool:
        """Whether any guard on the room is raised now."""
        room = self._rooms.get(room_id)
        return room is not None and any(self._is_on(entity) for entity in room.guards)

    def _apply(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._settings = config_mod.system(config)
        self.store.retention_days = self._settings["event_log_retention_days"]
        rooms = _rooms(config, self._guard_providers)
        watched_before = self._watched
        self._rooms = rooms
        self._bulbs = {
            entity: (room.id, bulb_id) for room in rooms.values() for bulb_id, entity in room.bulbs
        }
        self._guard_entities = {g for room in rooms.values() for g in room.guards}
        self._watched = (
            set(self._bulbs)
            | self._guard_entities
            | {s for room in rooms.values() for s in (*room.lux_sensors, *room.presence_sensors)}
        )
        for entity in set(self._states) - self._watched:
            del self._states[entity]
        if self._task is None or self._stopping:
            return
        if rooms and not self._subscribed:
            self._spawn(self._ensure_subscribed())
        elif self._watched - watched_before and self.connected:
            # Newly watched entities have no state in the mirror until it is re-read.
            self._spawn(self._refresh_quietly())

    async def _run(self) -> None:
        await self._ensure_subscribed()
        self._housekeeping()
        while True:
            minutes = self._settings["heartbeat_interval_minutes"]
            await asyncio.sleep(_until_next_tick(self._now(), minutes * 60))
            self.reload()
            await self._ensure_subscribed()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("an observation tick failed")
            self._housekeeping()

    async def _ensure_subscribed(self) -> None:
        if self._subscribed or not self._rooms:
            return
        self._subscribed = True  # registered for good: HA's client retries from here on
        try:
            await self.ha.subscribe_events("state_changed", self._on_event)
        except HAError as exc:
            # The ladder keeps trying, and says so when it gets through; a rejected token is
            # reported by the ladder's own "auth_failed".
            self._log(
                "warning",
                "connection",
                f"Home Assistant can't be reached yet ({exc}); the observer keeps trying.",
            )

    def _housekeeping(self) -> None:
        today = self._now().date()
        if self._pruned_on == today:
            return
        self._pruned_on = today
        try:
            pruned = self.store.prune_event_log()
        except Exception:  # noqa: BLE001 - housekeeping must not stop observation
            log.exception("pruning the event log failed")
            return
        if pruned:
            log.info("pruned %d event-log entries past retention", pruned)

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- HA's events -------------------------------------------------------------------

    async def _on_connection(self, state: str, detail: str | None) -> None:
        if state == "connected":
            first = not self._ever_connected
            self._ever_connected = True
            # Events were missed while away (or before the first subscribe): re-read.
            await self._refresh_quietly()
            self.connected = True
            if first:
                self._log(
                    "info",
                    "connection",
                    f"Observing {len(self._rooms)} room(s) and {len(self._bulbs)} bulb(s) on "
                    f"Home Assistant {detail or ''}".rstrip(),
                )
            else:
                self._log("info", "connection", "Reconnected to Home Assistant.")
        elif state == "disconnected":
            self.connected = False
            self._log(
                "warning", "connection", "Lost the connection to Home Assistant; reconnecting."
            )
        elif state == "auth_failed":
            self.connected = False
            self._log(
                "error",
                "connection",
                f"Home Assistant rejected the token, so observation has stopped: {detail}. "
                "Fix LM_HA_TOKEN (or LM_HA_TOKEN_FILE) and restart Light Map.",
            )

    async def _on_event(self, event: dict[str, Any]) -> None:
        """One `state_changed` from HA, in order."""
        data = event.get("data") or {}
        entity = data.get("entity_id")
        if entity not in self._watched:
            return
        self.events_seen += 1
        self.last_event_at = timestamp(self._now())
        before = _reading(data.get("old_state"))
        self._remember(entity, data.get("new_state"))
        if entity in self._guard_entities:
            self._signal_guards()
        where = self._bulbs.get(entity)
        if where is None:
            return
        room_id, bulb_id = where
        if is_user_intervention(event, guarded=self.guarded(room_id)):
            self._note_intervention(room_id, bulb_id, before)

    def _remember(self, entity: str, state: Any) -> None:
        """Keep `state` as the entity's in the mirror, unless the mirror holds a newer one.

        A fresh read of HA's states and the event stream run side by side, so either can
        bring news older than what the other just delivered; `last_updated` decides.
        """
        if not isinstance(state, Mapping):
            self._states.pop(entity, None)
            return
        held = self._states.get(entity)
        if held is not None and _updated(held) > _updated(state):
            return
        self._states[entity] = state

    def _signal_guards(self) -> None:
        signal, self._guard_signal = self._guard_signal, asyncio.Event()
        signal.set()

    async def _refresh_states(self) -> None:
        """Re-read every watched state from HA. Concurrent callers share one read."""
        task = self._refreshing
        if task is None or task.done():
            task = self._refreshing = asyncio.ensure_future(self._fetch_states())
        await asyncio.shield(task)

    async def _fetch_states(self) -> None:
        states = await self.ha.get_states()
        seen = set()
        for state in states:
            entity = state.get("entity_id") if isinstance(state, Mapping) else None
            if entity in self._watched:
                seen.add(entity)
                self._remember(entity, state)
        for entity in self._watched - seen:
            self._states.pop(entity, None)  # gone from HA
        if self._guard_entities:
            self._signal_guards()  # a guard may have dropped while it was away

    async def _refresh_quietly(self) -> None:
        try:
            await self._refresh_states()
        except HAError as exc:
            log.warning("could not re-read HA's states: %s", exc)

    # -- Heartbeats --------------------------------------------------------------------

    async def tick(self) -> list[dict[str, Any]]:
        """Take one observation tick for every observed room; return what was recorded."""
        try:
            await self._refresh_states()
        except HAError as exc:
            if not self._unreachable:
                self._unreachable = True
                self._log(
                    "warning",
                    "heartbeat",
                    f"Heartbeats are skipped while Home Assistant can't be read: {exc}",
                )
            return []
        if self._unreachable:
            self._unreachable = False
            self._log("info", "heartbeat", "Heartbeats resumed.")
        room_ids = sorted(self._rooms)
        results = await asyncio.gather(
            *(self._heartbeat(room_id, refreshed=True) for room_id in room_ids),
            return_exceptions=True,
        )
        recorded = []
        for room_id, result in zip(room_ids, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                self._log("error", "heartbeat", f"The heartbeat failed: {result}", room_id)
            elif result is not None:
                recorded.append(result)
        if recorded:
            readings = [r for o in recorded for r in o["readings"]]
            on = sum(1 for r in readings if r["is_on"])
            self._log(
                "debug",
                "heartbeat",
                f"Heartbeat: {len(recorded)} room(s), {len(readings)} bulb(s), {on} on.",
            )
        return recorded

    async def heartbeat(self, room_id: str) -> dict[str, Any] | None:
        """Take one observation tick for a room: one heartbeat plus one reading per bulb.

        Waits while a guard on the room is raised, and returns None (skipped, with a
        warning) if one is still up after the guard wait. Raises KeyError for a room that
        isn't observed (no bulbs, or not in the config).
        """
        if room_id not in self._rooms:
            raise KeyError(f"room '{room_id}' is not observed")
        return await self._heartbeat(room_id, refreshed=False)

    async def _heartbeat(self, room_id: str, *, refreshed: bool) -> dict[str, Any] | None:
        waited = await self._wait_unguarded(room_id, refreshed)
        room = self._rooms.get(room_id)
        if room is None:
            return None  # dropped from the config while it waited
        if waited is None:
            self._log(
                "warning",
                "heartbeat",
                f"Skipped a heartbeat: a guard was still raised after "
                f"{self._guard_wait:g} s, and a reading mid-transition would record a "
                "brightness nobody chose.",
                room_id,
            )
            return None
        lux, sensors = self._lux(room)
        heartbeat = {
            "ts": timestamp(self._now()),
            "room_id": room_id,
            "section": self._section(room_id),
            "lux": lux,
            "lux_sensors": sensors,
            "occupancy": self._occupancy(room),
            "deferred_ms": round(waited * 1000),
        }
        readings = []
        for bulb_id, entity in room.bulbs:
            is_on, brightness = self._reading(entity)
            readings.append({"bulb_id": bulb_id, "is_on": is_on, "brightness": brightness})
        self.store.append_heartbeat(heartbeat, readings)
        self.last_heartbeat_at = heartbeat["ts"]
        observation = {
            "kind": "heartbeat",
            "room_id": room_id,
            "heartbeat": heartbeat,
            "readings": readings,
        }
        await self._fan_out(observation)
        return observation

    async def _wait_unguarded(self, room_id: str, refreshed: bool) -> float | None:
        """Wait until no guard on the room is raised, with the states read fresh after
        any wait; return the seconds spent waiting, or None past the guard wait."""
        waited = 0.0
        while True:
            if self.guarded(room_id):
                remaining = self._guard_wait - waited
                if remaining <= 0:
                    return None
                signal = self._guard_signal
                started = time.monotonic()
                with suppress(TimeoutError):
                    await asyncio.wait_for(signal.wait(), remaining)
                waited += time.monotonic() - started
                refreshed = False
                continue
            if refreshed:
                return waited
            await self._refresh_states()
            refreshed = True  # and look again: the fresh read may show a guard raised

    # -- Reactive detection ------------------------------------------------------------

    def _note_intervention(self, room_id: str, bulb_id: str, before: Reading) -> None:
        window = self._windows.get(room_id)
        if window is None:
            room = self._rooms[room_id]
            window = _Window(
                opened=self._now(),
                seconds=self._settings["reactive_window_seconds"],
                section=self._section(room_id),
                lux_before=self._lux(room)[0],
                entities=dict(room.bulbs),
                before={b: self._reading(entity) for b, entity in room.bulbs},
            )
            self._windows[room_id] = window
            window.timer = asyncio.create_task(self._close_later(room_id, window))
        if bulb_id not in window.touched:
            # Where the user started from on this bulb: the change's own old state.
            window.touched.add(bulb_id)
            window.before[bulb_id] = before

    async def _close_later(self, room_id: str, window: _Window) -> None:
        await asyncio.sleep(window.seconds)
        await self._close_window(room_id, window)

    async def flush(self, *, everything: bool = False) -> None:
        """Close the reactive windows that are due (all of them with `everything`)."""
        now = self._now()
        for room_id, window in list(self._windows.items()):
            if everything or window.deadline <= now:
                await self._close_window(room_id, window)

    async def _close_window(self, room_id: str, window: _Window) -> None:
        if self._windows.get(room_id) is not window:
            return  # already closed
        del self._windows[room_id]
        if window.timer is not None and window.timer is not asyncio.current_task():
            window.timer.cancel()
        room = self._rooms.get(room_id)
        entities = {**window.entities, **(dict(room.bulbs) if room else {})}
        min_delta = self._settings["reactive_min_delta"]
        changes = []
        for bulb_id in sorted(entities):
            start = window.before.get(bulb_id, (None, None))
            end = self._reading(entities[bulb_id])
            changes.append(
                {
                    "bulb_id": bulb_id,
                    "is_on_before": start[0],
                    "is_on_after": end[0],
                    "brightness_before": start[1],
                    "brightness_after": end[1],
                    "changed": bulb_id in window.touched and _moved(start, end, min_delta),
                }
            )
        changed = [c["bulb_id"] for c in changes if c["changed"]]
        if not changed:
            log.debug("reactive window in %s closed with nothing past the min-delta", room_id)
            return
        event = {
            "ts": timestamp(window.opened),
            "room_id": room_id,
            "section": window.section,
            "window_seconds": window.seconds,
            "lux_before": window.lux_before,
            "lux_after": self._lux(room)[0] if room else None,
            # Recorded, never filtered on: a hand on a dimmer is proof of presence.
            "occupancy": self._occupancy(room) if room else None,
        }
        try:
            self.store.append_reactive_event(event, changes)
        except Exception as exc:  # noqa: BLE001 - one bad write must not stop observation
            log.exception("recording a reactive event failed")
            self._log("error", "reactive", f"A manual change could not be recorded: {exc}", room_id)
            return
        name = room.name if room else room_id
        self._log(
            "info",
            "reactive",
            f"Manual change in {name}: {', '.join(changed)}.",
            room_id,
            {
                c["bulb_id"]: [c["brightness_before"], c["brightness_after"]]
                for c in changes
                if c["changed"]
            },
        )
        await self._fan_out(
            {"kind": "reactive", "room_id": room_id, "event": event, "changes": changes}
        )

    # -- Readings ----------------------------------------------------------------------

    def _is_on(self, entity: str) -> bool:
        state = self._states.get(entity)
        return state is not None and state.get("state") == "on"

    def _reading(self, entity: str) -> Reading:
        return _reading(self._states.get(entity))

    def _lux(self, room: _Room) -> tuple[float | None, int]:
        """The room's lux, and how many sensors it came from. A reading of 0 is the
        sensor's floor, not a measurement (sensors sit at 0 all night with the lights on),
        so it never drags a real reading down; when every sensor reads 0, the lux is 0,
        the floor. None when no sensor has a reading."""
        values = []
        for entity in room.lux_sensors:
            state = self._states.get(entity)
            try:
                value = float(state.get("state")) if state else math.nan
            except (TypeError, ValueError):
                continue  # unavailable, unknown
            if math.isfinite(value) and value >= 0:
                values.append(value)
        measured = [v for v in values if v > 0]
        if measured:
            return round(sum(measured) / len(measured), 1), len(measured)
        if values:
            return 0.0, len(values)
        return None, 0

    def _occupancy(self, room: _Room) -> bool | None:
        """True if any presence sensor is on, False if every available one is off, None
        when none has a usable state (or none is configured): recorded, not assumed."""
        known = [
            state.get("state")
            for entity in room.presence_sensors
            if (state := self._states.get(entity)) is not None
            and state.get("state") not in (None, "unavailable", "unknown")
        ]
        if not known:
            return None
        return "on" in known

    def _section(self, room_id: str) -> str | None:
        if self._section_provider is None:
            return None
        try:
            return self._section_provider(room_id)
        except Exception:  # noqa: BLE001 - a module's failure must not stop observation
            log.exception("the section provider failed for %s", room_id)
            return None

    # -- Fan-out and the event log -----------------------------------------------------

    async def _fan_out(self, observation: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            try:
                await handler(observation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one module must not stop the others
                log.exception("an observation handler failed")
                self._log(
                    "error",
                    "observer",
                    f"A module failed to take a {observation['kind']} observation: {exc!r}",
                    observation.get("room_id"),
                )

    def _log(
        self,
        level: str,
        category: str,
        message: str,
        room_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Into the event log (and the container's log, from info up). Never raises: a
        log entry that can't be written must not stop observation."""
        log.log(_LEVELS.get(level, logging.ERROR), "%s", message)
        try:
            self.store.log_event(level, category, message, room_id=room_id, detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("could not write to the event log")


# -- Pure helpers ----------------------------------------------------------------------


def _rooms(config: Mapping[str, Any], providers: Iterable[GuardProvider]) -> dict[str, _Room]:
    """The rooms to observe: every room with bulbs."""
    providers = list(providers)
    rooms = {}
    for room in config.get("rooms") or []:
        bulbs = tuple(sorted((b["id"], b["entity_id"]) for b in room.get("bulbs") or []))
        if not bulbs:
            continue
        # The sensors live in the adaptive module's block, where the config shape keeps
        # them; absent before a room is adaptive, so such a room records no lux.
        sensors = room.get("adaptive_config") or {}
        guards: set[str] = set()
        for provider in providers:
            try:
                guards.update(provider(config, room) or ())
            except Exception:  # noqa: BLE001
                log.exception("a guard provider failed for %s", room.get("id"))
        rooms[room["id"]] = _Room(
            id=room["id"],
            name=room.get("name") or room["id"],
            bulbs=bulbs,
            lux_sensors=tuple(sensors.get("lux_sensors") or ()),
            presence_sensors=tuple(sensors.get("presence_sensors") or ()),
            guards=tuple(sorted(guards)),
        )
    return rooms


def _reading(state: Any) -> Reading:
    if not isinstance(state, Mapping):
        return None, None
    if state.get("state") == "on":
        return True, _brightness(state)
    if state.get("state") == "off":
        return False, None
    return None, None  # unavailable, unknown: never counted as off


def _brightness(state: Mapping[str, Any]) -> int | None:
    value = (state.get("attributes") or {}).get("brightness")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = round(value)
    return value if 0 <= value <= 255 else None


def _parent_id(item: Mapping[str, Any]) -> Any:
    context = item.get("context")
    return context.get("parent_id") if isinstance(context, Mapping) else None


def _updated(state: Mapping[str, Any]) -> datetime:
    """When HA last wrote the state; the oldest possible time when it doesn't say."""
    try:
        moment = datetime.fromisoformat(str(state.get("last_updated")))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _moved(start: Reading, end: Reading, min_delta: int) -> bool:
    """Whether a bulb ended somewhere other than it started: a different power state, or a
    brightness more than `min_delta` away. Not when either end is unknown."""
    (on_before, brightness_before), (on_after, brightness_after) = start, end
    if on_before is None or on_after is None:
        return False
    if on_before != on_after:
        return True
    if not on_after:
        return False
    if brightness_before is None or brightness_after is None:
        return brightness_before != brightness_after
    return abs(brightness_after - brightness_before) > min_delta


def _until_next_tick(now: datetime, interval: float) -> float:
    """Seconds to the next multiple of `interval` since the epoch, so a 10-minute cadence
    ticks at :00, :10, ... whenever the observer started."""
    return interval - (now.timestamp() % interval)
