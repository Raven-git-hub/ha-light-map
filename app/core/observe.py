"""Observation: the live observer: per-bulb heartbeats, reactive (manual-change) detection,
the event log. A platform service modules subscribe to. Readings are per bulb, long format.

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
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.ha import HAClient
from app.core.store import Store

ObservationHandler = Callable[[dict[str, Any]], Awaitable[None]]


def is_user_intervention(event: Mapping[str, Any], *, guarded: bool) -> bool:
    """Return True only for a brightness change with no `context.parent_id` while no guard
    is raised."""
    raise NotImplementedError


class Observer:
    """The platform observation service: one HA event subscription, fanned out to modules."""

    def __init__(self, ha: HAClient, store: Store, config: Mapping[str, Any]) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        """Start observing; the HA read loop must already be running."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Stop observing and flush anything pending to the store."""
        raise NotImplementedError

    def subscribe(self, handler: ObservationHandler) -> None:
        """Register a module's handler for heartbeats and reactive events."""
        raise NotImplementedError

    async def heartbeat(self, room_id: str) -> None:
        """Take one observation tick for a room: one heartbeat plus one reading per bulb."""
        raise NotImplementedError
