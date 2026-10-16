"""HA transport: Home Assistant REST + WebSocket, plus Area/entity-registry read & write.

Knows nothing about rooms, sections, or almanacs. The layers above turn HA's states,
events, and registries into the inventory and observations; this module is connection
plumbing and a thin surface over HA's APIs.

Two transports, used for different things:

- REST for request/response work: reading states, calling services, pushing states, and
  reading and writing automation configs (HA's config API for automations.yaml).
- WebSocket for the registries (HA exposes Area and entity-registry reads and writes only
  there), the UI-managed helpers (`input_boolean/list`, `.../create`, ...: the frontend's
  own commands, the only network API that creates a helper), and, from P4, the live event
  stream.

Hard rules:

- The WebSocket read loop must start before subscribing. Subscribing first deadlocks until
  timeout and leaves the event stream dead while the log still says "connected."
- Entity IDs only, never device IDs. Device IDs are opaque, break on re-pairing, and can't
  be validated by an entity-existence check. The device registry is read only so the
  inventory can resolve the Area an entity inherits from its device, and so deploy can
  check that a hard switch's `device` source still exists; device IDs never leave those
  reads. (The one place a device id reaches HA is inside a generated hard-switch trigger,
  which only reads a button press; see `core.switching`.)
- The HA token is never persisted in the config. At runtime it is read from LM_HA_TOKEN, or
  from the file named by LM_HA_TOKEN_FILE.
- Area/entity-registry writes are a new write surface (the predecessor only created helpers
  and automations and pushed states). They get the same care as helper deploy: verified
  after the fact by the caller (`core.inventory` for edits from the Map, `core.deploy` for
  deploys), reconcilable, never destructive by surprise.

`subscribe_events` (and the reconnect ladder that keeps subscriptions alive) arrives with
the observer in P4. Until then a dropped WebSocket is simply reopened by the next command.
"""

import asyncio
import json
import logging
import os
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_TIMEOUT = 10.0

# A large install's entity registry arrives as one result message that can exceed
# websockets' 1 MiB default max_size. Hitting that limit closes the socket (code 1009)
# mid-read, which looks like a flaky connection rather than a size limit.
MAX_MESSAGE_BYTES = 64 * 2**20

# Guards on values interpolated into REST paths, so a malformed id can't address another
# endpoint (e.g. "../config").
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SLUG = re.compile(r"^[a-z0-9_]+$")


class HAError(RuntimeError):
    """Home Assistant could not be reached, or refused or failed a request."""


class HAAuthError(HAError):
    """Token rejected. Never retried: retrying a bad token just spams the log and delays
    the user discovering the real problem."""


class HANotFoundError(HAError):
    """A REST request answered 404: what it asked for doesn't exist."""


class HACommandError(HAError):
    """A WebSocket command came back with `success: false`. `code` is HA's error code
    (e.g. `not_found`, `invalid_format`, `unauthorized`)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def token_from_env() -> str:
    """Return the HA token from LM_HA_TOKEN, or from the file named by LM_HA_TOKEN_FILE."""
    # An empty LM_HA_TOKEN counts as unset. .env.example ships that line blank and compose's
    # env_file then sets it to "", which would otherwise shadow LM_HA_TOKEN_FILE and surface
    # as a baffling 401.
    token = os.environ.get("LM_HA_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("LM_HA_TOKEN_FILE", "").strip()
    if not token_file:
        raise HAError(
            "no Home Assistant token: set LM_HA_TOKEN, or LM_HA_TOKEN_FILE to a file holding it"
        )
    try:
        # Stripped: a secrets file almost always ends in a newline, and HA rejects the
        # token with it attached.
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HAError(f"cannot read LM_HA_TOKEN_FILE {token_file}: {exc.strerror}") from exc
    if not token:
        raise HAError(f"LM_HA_TOKEN_FILE {token_file} is empty")
    return token


def _websocket_url(base_url: str) -> str:
    scheme, sep, rest = base_url.partition("://")
    if sep and scheme == "https":
        return f"wss://{rest}/api/websocket"
    if sep and scheme == "http":
        return f"ws://{rest}/api/websocket"
    raise HAError(f"Home Assistant URL must start with http:// or https://: {base_url!r}")


class HAClient:
    """One connection to one Home Assistant instance: REST requests and a WebSocket session.

    Construction does no I/O. The HTTP client is created on first request and the
    WebSocket is opened by `connect()`, or on demand by the first command. Use as an async
    context manager, or call `close()`.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` replaces httpx's network transport; tests pass a mock."""
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.ha_version: str | None = None
        self._token = token
        self._ws_url = _websocket_url(self.base_url)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._last_id = 0
        self._connect_lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Deliberately leaves the token out, so a logged client can't leak it.
        return f"HAClient({self.base_url!r}, connected={self.connected})"

    async def __aenter__(self) -> "HAClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        """True while the WebSocket is authenticated and its read loop is running."""
        return self._ws is not None and self._reader is not None and not self._reader.done()

    # -- WebSocket ---------------------------------------------------------------------

    async def connect(self) -> None:
        """Open and authenticate the WebSocket, then start its read loop before anything
        can subscribe."""
        async with self._connect_lock:
            if self.connected:
                return
            kwargs: dict[str, Any] = {}
            if self._ws_url.startswith("wss://") and not self.verify_ssl:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                kwargs["ssl"] = context
            try:
                ws = await ws_connect(
                    self._ws_url,
                    max_size=MAX_MESSAGE_BYTES,
                    open_timeout=self.timeout,
                    **kwargs,
                )
            except (OSError, TimeoutError, WebSocketException) as exc:
                raise HAError(f"cannot open the WebSocket at {self._ws_url}: {exc}") from exc
            try:
                await self._authenticate(ws)
            except BaseException:
                await ws.close()
                raise
            self._ws = ws
            # The reader must be running before any command is sent. Every command awaits
            # its result message, and only the read loop can deliver one: subscribing first
            # deadlocks until the timeout, drops the connection, and reconnects into the
            # same trap, leaving the event stream permanently dead while the log cheerfully
            # reports "connected".
            self._reader = asyncio.create_task(self._read_loop(ws))
            log.info("connected to Home Assistant %s", self.ha_version)

    async def _authenticate(self, ws: ClientConnection) -> None:
        greeting = await self._receive(ws)
        if greeting.get("type") != "auth_required":
            raise HAError(f"unexpected WebSocket greeting: {greeting.get('type')!r}")
        self.ha_version = greeting.get("ha_version")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = await self._receive(ws)
        if reply.get("type") == "auth_invalid":
            raise HAAuthError(reply.get("message") or "Home Assistant rejected the access token")
        if reply.get("type") != "auth_ok":
            raise HAError(f"unexpected WebSocket auth reply: {reply.get('type')!r}")
        self.ha_version = reply.get("ha_version", self.ha_version)

    async def _receive(self, ws: ClientConnection) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(ws.recv(), self.timeout)
        except TimeoutError:
            raise HAError("Home Assistant did not answer the WebSocket handshake") from None
        except ConnectionClosed as exc:
            raise HAError(f"WebSocket closed during authentication: {exc}") from exc
        try:
            message = json.loads(raw)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            raise HAError("unexpected WebSocket message during authentication")
        return message

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    log.warning("ignoring a WebSocket message that is not JSON")
                    continue
                # HA sends a JSON array only when the client opts into message coalescing;
                # this client doesn't, but accepting both costs nothing.
                for item in message if isinstance(message, list) else [message]:
                    if isinstance(item, dict):
                        self._dispatch(item)
        except ConnectionClosed as exc:
            log.warning("WebSocket closed: %s", exc)
        finally:
            # Fail every waiter now. Otherwise each one sits out its full timeout on a
            # socket that will never answer.
            self._fail_pending(HAError("the WebSocket connection to Home Assistant closed"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in ("result", "pong"):
            future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
            if future is None or future.done():
                return
            if kind == "result" and message.get("success") is not True:
                error = message.get("error") or {}
                future.set_exception(
                    HACommandError(
                        str(error.get("code", "unknown_error")),
                        str(error.get("message", "command failed")),
                    )
                )
            else:
                future.set_result(message.get("result"))
        # "event" messages need a subscription, and subscriptions arrive in P4.

    def _fail_pending(self, error: HAError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        """Stop the read loop and close the WebSocket and the HTTP client."""
        ws, self._ws = self._ws, None
        reader, self._reader = self._reader, None
        if ws is not None:
            await ws.close()
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        self._fail_pending(HAError("the Home Assistant client was closed"))
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    async def command(self, message: dict[str, Any], *, timeout: float | None = None) -> Any:
        """Send a WebSocket command and wait for its result.

        Opens the WebSocket first if it isn't open (or has dropped). Raises
        HACommandError if HA answers `success: false`, HAError if no answer arrives.
        """
        if "id" in message:
            raise ValueError("the client assigns WebSocket message ids")
        await self.connect()
        ws = self._ws
        assert ws is not None
        self._last_id += 1
        message_id = self._last_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        kind = message.get("type")
        try:
            await ws.send(json.dumps({**message, "id": message_id}))
            return await asyncio.wait_for(future, timeout or self.timeout)
        except ConnectionClosed as exc:
            raise HAError(f"the WebSocket closed while sending {kind}: {exc}") from exc
        except TimeoutError:
            raise HAError(f"Home Assistant did not answer {kind} in time") from None
        finally:
            self._pending.pop(message_id, None)

    async def subscribe_events(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe `handler` to an HA event type. The read loop must already be running.

        Arrives with the observer (P4), together with the reconnect ladder that
        re-establishes subscriptions after a drop: a dropped connection silently loses them.
        """
        raise NotImplementedError

    # -- REST --------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                verify=self.verify_ssl,
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._http

    async def request(self, method: str, path: str, json: Any = None) -> Any:
        """Send a REST request to the HA API and return the decoded response."""
        if not path.startswith("/api/"):
            raise ValueError(f"not an HA API path: {path!r}")
        try:
            response = await self._client().request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise HAError(f"{method} {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise HAAuthError("Home Assistant rejected the access token")
        if response.status_code >= 400:
            # HA explains a refused write in the body (e.g. the automation config API's
            # "Message malformed: ..."); without it a failed deploy says only "HTTP 400".
            detail = _error_message(response)
            error = HANotFoundError if response.status_code == 404 else HAError
            raise error(
                f"{method} {path} returned HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    async def ping(self) -> str:
        """GET /api/: the cheapest proof that the URL and the token both work."""
        data = await self.request("GET", "/api/")
        return str(data.get("message", "")) if isinstance(data, dict) else str(data)

    async def get_config(self) -> dict[str, Any]:
        """Return HA's core config: version, location, time zone."""
        return await self.request("GET", "/api/config")

    async def get_states(self) -> list[dict[str, Any]]:
        """Return the current state object of every entity."""
        return await self.request("GET", "/api/states")

    async def set_state(
        self, entity_id: str, state: str, attributes: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Write an entity state over REST. This is how the adaptive almanac is pushed.

        A state set this way does not survive an HA restart, so whoever pushes it must
        re-push after one.
        """
        if not _ENTITY_ID.match(entity_id):
            raise ValueError(f"not an entity id: {entity_id!r}")
        return await self.request(
            "POST",
            f"/api/states/{entity_id}",
            json={"state": state, "attributes": dict(attributes)},
        )

    async def call_service(self, domain: str, service: str, data: Mapping[str, Any]) -> Any:
        """Call an HA service; returns the states it changed."""
        if not (_SLUG.match(domain) and _SLUG.match(service)):
            raise ValueError(f"not a service: {domain!r}.{service!r}")
        return await self.request("POST", f"/api/services/{domain}/{service}", json=dict(data))

    # -- Registries (WebSocket only) ---------------------------------------------------

    async def list_areas(self) -> list[dict[str, Any]]:
        """Return the Area registry: `{area_id, name, ...}` per Area."""
        return await self.command({"type": "config/area_registry/list"})

    async def create_area(self, name: str) -> dict[str, Any]:
        """Create an HA Area and return its registry entry."""
        return await self.command({"type": "config/area_registry/create", "name": name})

    async def rename_area(self, area_id: str, name: str) -> dict[str, Any]:
        """Rename an HA Area and return its updated registry entry."""
        return await self.command(
            {"type": "config/area_registry/update", "area_id": area_id, "name": name}
        )

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        """Return the entity registry, which carries each entity's own Area assignment and
        its entry `id` (HA's stable handle for the entry, kept across entity_id renames).

        An entity with no Area of its own inherits its device's (see
        `list_device_registry`).
        """
        return await self.command({"type": "config/entity_registry/list"})

    async def get_entity_registry_entry(self, entity_id: str) -> dict[str, Any] | None:
        """Return one entity's registry entry, or None if the registry doesn't have it
        (e.g. a YAML-defined light with no unique_id)."""
        try:
            return await self.command(
                {"type": "config/entity_registry/get", "entity_id": entity_id}
            )
        except HACommandError as exc:
            if exc.code == "not_found":
                return None
            raise

    async def set_entity_area(self, entity_id: str, area_id: str | None) -> dict[str, Any]:
        """Assign an entity to an Area, or clear its Area when `area_id` is None; return
        the updated registry entry.

        Clearing an entity's own Area makes it fall back to its device's Area, if the
        device has one.
        """
        result = await self.command(
            {"type": "config/entity_registry/update", "entity_id": entity_id, "area_id": area_id}
        )
        if isinstance(result, dict) and isinstance(result.get("entity_entry"), dict):
            return result["entity_entry"]
        return result

    async def list_device_registry(self) -> list[dict[str, Any]]:
        """Return the device registry. Read only to resolve the Area an entity inherits
        from its device (and to check a hard switch's device source exists); device IDs
        must not travel beyond those reads."""
        return await self.command({"type": "config/device_registry/list"})

    # -- Helpers (WebSocket) and automations (REST): what deploy writes -----------------

    async def list_helpers(self, domain: str) -> list[dict[str, Any]]:
        """Return the UI-managed helpers of one domain (e.g. `input_boolean`), each its
        stored item (`id`, `name`, ...). Helpers defined in YAML are not listed: they live
        outside HA's storage collection and can't be edited over the network."""
        return await self.command({"type": f"{_helper_domain(domain)}/list"})

    async def create_helper(self, domain: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """Create a UI-managed helper and return its stored item.

        There is no way to ask for an id: HA derives the item's `id`, and the entity's
        object id, by slugifying `name`, and appends `_2`, `_3`, ... when that id is taken.
        A caller that needs a particular entity_id must choose a name that slugifies to it
        and check the `id` that comes back.
        """
        domain = _helper_domain(domain)
        return await self.command({**_helper_fields(domain, data), "type": f"{domain}/create"})

    async def update_helper(
        self, domain: str, item_id: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Replace a UI-managed helper's fields (a field left out is cleared) and return
        the stored item. Its id and entity_id don't change."""
        domain = _helper_domain(domain)
        return await self.command(
            {**_helper_fields(domain, data), "type": f"{domain}/update", f"{domain}_id": item_id}
        )

    async def delete_helper(self, domain: str, item_id: str) -> None:
        """Delete a UI-managed helper, and with it its entity."""
        domain = _helper_domain(domain)
        await self.command({"type": f"{domain}/delete", f"{domain}_id": item_id})

    async def get_automation_config(self, automation_id: str) -> dict[str, Any] | None:
        """Return an automation's stored config (from automations.yaml) by its `id`, or
        None if HA's config API doesn't have it (no such id, or one defined elsewhere in
        YAML, which the API can't edit)."""
        try:
            return await self.request("GET", _automation_path(automation_id))
        except HANotFoundError:
            return None

    async def save_automation_config(self, automation_id: str, config: Mapping[str, Any]) -> None:
        """Create or replace an automation in automations.yaml by its `id`.

        HA validates the config first (HTTP 400 with the reason if it is malformed), then
        writes it and reloads that automation in the background: the automation entity
        can lag the reply by a moment, so a caller verifying the result should retry.
        """
        await self.request("POST", _automation_path(automation_id), json=dict(config))

    async def delete_automation_config(self, automation_id: str) -> bool:
        """Remove an automation from automations.yaml; False if it wasn't there. Like a
        save, the entity goes away with a background reload."""
        try:
            await self.request("DELETE", _automation_path(automation_id))
        except HANotFoundError:
            return False
        return True


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return ""


def _helper_domain(domain: str) -> str:
    if not _SLUG.match(domain):
        raise ValueError(f"not a helper domain: {domain!r}")
    return domain


def _helper_fields(domain: str, data: Mapping[str, Any]) -> dict[str, Any]:
    # The command's own keys are the client's to set: a field named `type` or
    # `<domain>_id` would silently turn the command into a different one.
    reserved = {"type", "id", f"{domain}_id"} & set(data)
    if reserved:
        raise ValueError(f"helper fields may not include {sorted(reserved)}")
    return dict(data)


def _automation_path(automation_id: str) -> str:
    if not _SLUG.match(automation_id):
        raise ValueError(f"not an automation id: {automation_id!r}")
    return f"/api/config/automation/config/{automation_id}"
