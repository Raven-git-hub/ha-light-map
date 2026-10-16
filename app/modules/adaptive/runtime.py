"""Adaptive runtime: the adaptive loop: subscribe to core observation, run the scheduler,
fire crossovers, push the almanac.

Hard rules:

- Observation comes only from the `core.observe` subscription; the loop never scrapes HA.
- Actuation is only by commanding soft switches (power) and setting brightness guarded on
  soft-on, never by powering bulbs directly.
- Scene automations are fired via `automation.trigger`, which skips conditions; that is why
  they carry `conditions: []`.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.core.ha import HAClient
from app.core.observe import Observer
from app.core.store import Store


class AdaptiveRuntime:
    """The Adaptive Light loop for every room flagged adaptive."""

    def __init__(
        self, ha: HAClient, observer: Observer, store: Store, config: Mapping[str, Any]
    ) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        """Subscribe to observation and start the scheduler loop."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Stop the loop."""
        raise NotImplementedError

    async def on_observation(self, observation: dict[str, Any]) -> None:
        """Handle one heartbeat or reactive event from the platform observer."""
        raise NotImplementedError

    async def tick(self, now: datetime) -> None:
        """Evaluate each adaptive room's section at `now` and fire any crossover due."""
        raise NotImplementedError

    async def publish_almanac(self, room_id: str) -> None:
        """Push a room's current almanac to HA."""
        raise NotImplementedError
