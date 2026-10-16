"""HA transport: Home Assistant REST + WebSocket, plus Area/entity-registry read & write.

Knows nothing about rooms, sections, or almanacs. The layers above turn HA's states,
events, and registries into the inventory and observations; this module is connection
plumbing and a thin surface over HA's APIs.

Hard rules:

- The WebSocket read loop must start before subscribing. Subscribing first deadlocks until
  timeout and leaves the event stream dead while the log still says "connected."
- Entity IDs only, never device IDs. Device IDs are opaque, break on re-pairing, and can't
  be validated by an entity-existence check.
- The HA token is never persisted in the config. At runtime it is read from LM_HA_TOKEN, or
  from the file named by LM_HA_TOKEN_FILE.
- Area/entity-registry writes are a new write surface (the predecessor only created helpers
  and automations and pushed states). They get the same care as helper deploy: verified
  after the fact (by `core.deploy`), reconcilable, never destructive by surprise.
"""

from collections.abc import Awaitable, Callable
from typing import Any

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


def token_from_env() -> str:
    """Return the HA token from LM_HA_TOKEN, or from the file named by LM_HA_TOKEN_FILE."""
    raise NotImplementedError


class HAClient:
    """One connection to one Home Assistant instance: REST requests and a WebSocket session."""

    def __init__(self, base_url: str, token: str, *, verify_ssl: bool = True) -> None:
        raise NotImplementedError

    async def connect(self) -> None:
        """Open and authenticate the WebSocket, then start its read loop before anything
        can subscribe."""
        raise NotImplementedError

    async def close(self) -> None:
        """Stop the read loop and close the WebSocket and the HTTP client."""
        raise NotImplementedError

    async def request(self, method: str, path: str, json: Any = None) -> Any:
        """Send a REST request to the HA API and return the decoded response."""
        raise NotImplementedError

    async def command(self, message: dict[str, Any]) -> Any:
        """Send a WebSocket command and wait for its result."""
        raise NotImplementedError

    async def subscribe_events(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe `handler` to an HA event type. The read loop must already be running."""
        raise NotImplementedError

    async def get_states(self) -> list[dict[str, Any]]:
        """Return the current state object of every entity."""
        raise NotImplementedError

    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        """Write an entity state over REST. This is how the adaptive almanac is pushed."""
        raise NotImplementedError

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Call an HA service."""
        raise NotImplementedError

    async def list_areas(self) -> list[dict[str, Any]]:
        """Return the Area registry."""
        raise NotImplementedError

    async def create_area(self, name: str) -> dict[str, Any]:
        """Create an HA Area and return its registry entry."""
        raise NotImplementedError

    async def rename_area(self, area_id: str, name: str) -> None:
        """Rename an HA Area."""
        raise NotImplementedError

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        """Return the entity registry, which carries each entity's Area assignment."""
        raise NotImplementedError

    async def set_entity_area(self, entity_id: str, area_id: str | None) -> None:
        """Assign an entity to an Area, or clear its Area when `area_id` is None."""
        raise NotImplementedError
