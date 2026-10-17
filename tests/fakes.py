"""Handwritten HA fixtures and a fake HAClient. Nothing here touches a network.

The fixture house, as HA would report it:

- Areas: Living Room, Kitchen, Garage (no lights).
- light.chandelier_1..3: no Area of their own, inherit Living Room from their device.
- light.reading_lamp: its own Area (Living Room); its device has none.
- light.counter_lamp: its own Area (Kitchen) overrides its device's (Living Room).
- light.kitchen_strip: inherits Kitchen from its device.
- light.porch: in the registry, but neither it nor its device has an Area.
- light.legacy_yaml: a YAML light with no unique_id, so no registry entry at all.
- light.living_room_group: an HA light group (lists member entity_ids).
- Dimmability (`supported_color_modes`, for the adaptive editor): the chandeliers, the
  reading lamp and the kitchen strip dim; the counter lamp only switches on and off; the
  porch light doesn't say; chandelier_3 is unavailable.
- sensor.living_room_lux: not a light.
- switch.reading_relay, event.living_room_button: hard-switch sources, not lights.

FakeHA also keeps UI-managed helpers and automations.yaml, answers service calls on its
helpers, and records every automation a state change would trigger (`fired`), matching
state triggers the way HA does (`state_trigger_matches`), so a test can see whether a
deploy would have driven a bulb.

For the observer it speaks the event side too: `subscribe_events` (announcing "connected"
to the connection listeners the first time), `change` to make a state change and get the
`state_changed` event HA would send, `emit` to deliver one, and `announce` for a drop or a
reconnect.
"""

import copy
import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

AREAS: list[dict[str, Any]] = [
    {"area_id": "living_room", "name": "Living Room", "aliases": [], "floor_id": None},
    {"area_id": "kitchen", "name": "Kitchen", "aliases": [], "floor_id": None},
    {"area_id": "garage", "name": "Garage", "aliases": [], "floor_id": None},
]

DEVICES: list[dict[str, Any]] = [
    {"id": "dev_chandelier", "name": "Chandelier", "area_id": "living_room"},
    {"id": "dev_reading", "name": "Reading lamp", "area_id": None},
    {"id": "dev_counter", "name": "Counter lamp", "area_id": "living_room"},
    {"id": "dev_strip", "name": "Kitchen strip", "area_id": "kitchen"},
    {"id": "dev_porch", "name": "Porch", "area_id": None},
]


def registry_id(entity_id: str) -> str:
    """The fixture registry's entry id for an entity, as first registered. It stays put if
    the entity_id is later renamed, as HA's does."""
    return f"reg_{entity_id.replace('.', '_')}"


def _entity(entity_id: str, device_id: str | None, area_id: str | None) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "id": registry_id(entity_id),
        "device_id": device_id,
        "area_id": area_id,
        "platform": "hue",
        "disabled_by": None,
        "hidden_by": None,
        "name": None,
    }


ENTITIES: list[dict[str, Any]] = [
    _entity("light.chandelier_1", "dev_chandelier", None),
    _entity("light.chandelier_2", "dev_chandelier", None),
    _entity("light.chandelier_3", "dev_chandelier", None),
    _entity("light.reading_lamp", "dev_reading", "living_room"),
    _entity("light.counter_lamp", "dev_counter", "kitchen"),
    _entity("light.kitchen_strip", "dev_strip", None),
    _entity("light.porch", "dev_porch", None),
    _entity("light.living_room_group", None, "living_room"),
    _entity("sensor.living_room_lux", "dev_chandelier", None),
]


def _state(eid: str, name: str, state: str = "off", **attributes: Any) -> dict[str, Any]:
    # `eid`, not `entity_id`: a light group's attributes carry an `entity_id` of their own.
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {"friendly_name": name, **attributes},
        "context": {"id": "ctx", "parent_id": None, "user_id": None},
    }


# What each light can do (`supported_color_modes`), for the adaptive editor's dimmability
# guard: the chandeliers and the strip take colour, the reading lamp only dims, the counter
# lamp only switches (on/off only: can't be adaptive), the porch light doesn't say (the
# attribute is missing: capability unknown), and the unavailable chandelier_3 reports
# nothing (offline: capability unknown).
STATES: list[dict[str, Any]] = [
    _state(
        "light.chandelier_1",
        "Chandelier 1",
        "on",
        brightness=128,
        supported_color_modes=["color_temp", "xy"],
    ),
    _state(
        "light.chandelier_2",
        "Chandelier 2",
        "on",
        brightness=128,
        supported_color_modes=["color_temp", "xy"],
    ),
    _state("light.chandelier_3", "Chandelier 3", "unavailable"),
    _state("light.reading_lamp", "Reading lamp", supported_color_modes=["brightness"]),
    _state("light.counter_lamp", "Counter lamp", supported_color_modes=["onoff"]),
    _state("light.kitchen_strip", "Kitchen strip", supported_color_modes=["hs"]),
    _state("light.porch", "Porch"),
    _state("light.legacy_yaml", "Legacy YAML light"),
    _state(
        "light.living_room_group",
        "Living room lights",
        entity_id=["light.chandelier_1", "light.chandelier_2", "light.chandelier_3"],
    ),
    _state("sensor.living_room_lux", "Living room lux", "42", device_class="illuminance"),
    _state("switch.reading_relay", "Reading relay"),
    _state("event.living_room_button", "Living room button", "2026-10-15T21:04:11.000+00:00"),
]


_REGISTERED = object()


def light(
    entity_id: str,
    area_id: str | None,
    name: str | None = None,
    *,
    is_group: bool = False,
    key: Any = _REGISTERED,
) -> dict[str, Any]:
    """A discovered-light record, as `inventory.discover_lights` returns it. `key` defaults
    to the fixture registry id; pass None for a light outside the registry."""
    return {
        "entity_id": entity_id,
        "ha_match_key": registry_id(entity_id) if key is _REGISTERED else key,
        "name": name or entity_id.partition(".")[2].replace("_", " ").capitalize(),
        "state": "off",
        "area_id": area_id,
        "area_inherited": False,
        "in_registry": True,
        "is_group": is_group,
    }


def bulb(
    bulb_id: str,
    entity_id: str | None = None,
    name: str | None = None,
    *,
    key: Any = _REGISTERED,
    **extra: Any,
) -> dict[str, Any]:
    """A snapshot bulb, keyed by the fixture registry id unless `key` says otherwise, and not
    ignored unless `ignored=True` is passed."""
    entity_id = entity_id or f"light.{bulb_id}"
    result: dict[str, Any] = {
        "id": bulb_id,
        "name": name or bulb_id.replace("_", " ").capitalize(),
        "entity_id": entity_id,
    }
    key = registry_id(entity_id) if key is _REGISTERED else key
    if key:
        result["ha_match_key"] = key
    return {**result, "ignored": False, "hard_switches": [], **extra}


def without_keys(doc: dict[str, Any]) -> dict[str, Any]:
    """The same snapshot as P1 wrote it, before bulbs carried a match key."""
    doc = copy.deepcopy(doc)
    for holder in [*doc["rooms"], {"bulbs": doc["unassigned"]}]:
        for item in holder["bulbs"]:
            item.pop("ha_match_key", None)
    return doc


SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


def scenes(*unit_ids: str, **modes: str) -> dict[str, Any]:
    """A scene matrix at the predecessor's defaults for these learning units, written out
    (not taken from `config.scene_defaults`, so the fixture checks the defaults rather than
    echoing them). `modes` sets a unit's mode in every section (`chandelier="off"`)."""
    return {
        section: {
            "lux_target_seed": None,
            "lux_margin": 5,
            "max_step_pct": 0.04,
            "maintenance_enabled": section not in ("night", "sleep"),
            "transition_seconds": 120,
            "units": {unit_id: {"mode": modes.get(unit_id, "auto")} for unit_id in unit_ids},
        }
        for section in SECTIONS
    }


def committed_config() -> dict[str, Any]:
    """A valid committed config for the fixture house, as of some earlier sync. Its bulbs
    carry match keys; `without_keys` gives the P1-era equivalent. The living room is
    adaptive, with every bulb adaptive in two learning units; `v3_config` gives it as
    version 3 wrote it."""
    return {
        "version": 4,
        "homeassistant": {"base_url": "http://ha.local:8123", "verify_ssl": True},
        "system": {},
        "rooms": [
            {
                "id": "living_room",
                "name": "Living Room",
                "ha_area_id": "living_room",
                "adaptive": True,
                "bulbs": [
                    bulb("chandelier_1"),
                    bulb("chandelier_2"),
                    bulb("chandelier_3"),
                    bulb(
                        "reading_lamp",
                        hard_switches=[
                            {
                                "id": "reading_switch",
                                "name": "Reading switch",
                                "source": {"type": "state", "entity_id": "switch.reading_relay"},
                                "mode": "follow",
                            }
                        ],
                    ),
                ],
                "switching_groups": [
                    {
                        "id": "living_all",
                        "name": "All living room lights",
                        "members": ["chandelier_1", "chandelier_2", "chandelier_3", "reading_lamp"],
                        "hard_switches": [
                            {
                                "id": "living_all_off",
                                "name": "All off button",
                                "source": {
                                    "type": "state",
                                    "entity_id": "event.living_room_button",
                                },
                                "mode": "press",
                                "turn": "off",
                            }
                        ],
                    },
                    {
                        "id": "reading_corner",
                        "name": "Reading corner",
                        "members": ["reading_lamp"],
                        "hard_switches": [],
                    },
                ],
                "adaptive_config": {
                    "lux_sensors": ["sensor.living_room_lux"],
                    "presence_sensors": [],
                    "schedule_profile": "evening",
                    "adaptive_bulbs": [
                        "chandelier_1",
                        "chandelier_2",
                        "chandelier_3",
                        "reading_lamp",
                    ],
                    "learning_units": [
                        {
                            "id": "chandelier",
                            "name": "Chandelier",
                            "members": ["chandelier_1", "chandelier_2", "chandelier_3"],
                        },
                        {"id": "reading", "name": "Reading", "members": ["reading_lamp"]},
                    ],
                    "scenes": scenes("chandelier", "reading"),
                },
            },
            {
                "id": "kitchen",
                "name": "Kitchen",
                "ha_area_id": "kitchen",
                "adaptive": False,
                "bulbs": [bulb("kitchen_strip")],
                "switching_groups": [],
            },
        ],
        "unassigned": [bulb("porch")],
        "schedule_profiles": [{"id": "default", "name": "Default"}, {"id": "evening"}],
        "learning": {},
    }


def v3_config() -> dict[str, Any]:
    """`committed_config` as P4.1 (schema version 3) wrote it: an adaptive config with no
    `adaptive_bulbs` (a learning unit was what named a bulb for the module) and the open
    `scenes` object nothing filled."""
    doc = committed_config()
    doc["version"] = 3
    adaptive_config = doc["rooms"][0]["adaptive_config"]
    del adaptive_config["adaptive_bulbs"]
    adaptive_config["scenes"] = {}
    return doc


def v2_config() -> dict[str, Any]:
    """`committed_config` as P2-P4 (schema version 2) wrote it: bulbs with no `ignored`."""
    doc = v3_config()
    doc["version"] = 2
    for holder in [*doc["rooms"], {"bulbs": doc["unassigned"]}]:
        for item in holder["bulbs"]:
            del item["ignored"]
    return doc


def v1_config() -> dict[str, Any]:
    """`committed_config` as P1 (schema version 1) wrote it: hard-switch sources as bare
    entity_ids, a press with no direction, and bulbs with no `ignored`."""
    doc = v2_config()
    doc["version"] = 1
    living = doc["rooms"][0]
    living["bulbs"][3]["hard_switches"][0]["source"] = "switch.reading_relay"
    binding = living["switching_groups"][0]["hard_switches"][0]
    binding["source"] = "event.living_room_button"
    del binding["turn"]
    return doc


WRITES = (
    "set_entity_area",
    "create_area",
    "rename_area",
    "create_helper",
    "update_helper",
    "delete_helper",
    "save_automation_config",
    "delete_automation_config",
    "call_service",
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def state_trigger_matches(trigger: dict[str, Any], entity_id: str, old: Any, new: Any) -> bool:
    """Would an HA state trigger fire for `entity_id` going from state `old` to `new`
    (None: the entity didn't exist / was removed)? HA's rules: from/to must match, not_from/
    not_to must not, and with any of them given, a change that keeps the state (attributes
    only) never fires."""
    if trigger.get("trigger", trigger.get("platform")) != "state":
        return False
    if entity_id not in _as_list(trigger.get("entity_id")):
        return False
    for value, wanted, unwanted in ((old, "from", "not_from"), (new, "to", "not_to")):
        if wanted in trigger and value not in _as_list(trigger[wanted]):
            return False
        if unwanted in trigger and value in _as_list(trigger[unwanted]):
            return False
    return old != new


def _slugify(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


class FakeHA:
    """Stands in for HAClient: serves the fixtures, records every call, and applies
    registry writes to its own state so that re-reads (verification) see them.

    `drop_writes` acknowledges writes without applying them, like a write that didn't land.
    """

    def __init__(
        self,
        areas: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        states: list[dict[str, Any]] | None = None,
        *,
        admin: bool = True,
        drop_writes: bool = False,
        helpers: dict[str, list[dict[str, Any]]] | None = None,
        automations: list[dict[str, Any]] | None = None,
        yaml_helpers: tuple[str, ...] = (),
        yaml_automations: tuple[str, ...] = (),
        rejected: tuple[str, ...] = (),
        disabled: tuple[str, ...] = (),
        lag: int = 0,
        related: dict[str, dict[str, list[str]]] | None = None,
        search_fails: bool = False,
    ) -> None:
        """`helpers` and `automations` pre-populate the UI-managed helpers and
        automations.yaml. `yaml_helpers` are helper ids taken by YAML helpers (unlisted,
        but a create can't have them); `yaml_automations` are automation ids defined in
        YAML outside automations.yaml (an entity, but no config the API can see).
        `rejected` automations load as `unavailable`; `disabled` ones have a config but no
        entity (disabled in HA's entity registry). `lag` is how many state reads a
        written or deleted automation's entity takes to catch up, as HA reloads it in the
        background. `related` adds to what `search_related` finds for an entity (beyond
        the automations.yaml entries that name it); `search_fails` makes it fail, as on an
        HA without the command."""
        self.areas = copy.deepcopy(AREAS if areas is None else areas)
        self.entities = copy.deepcopy(ENTITIES if entities is None else entities)
        self.devices = copy.deepcopy(DEVICES if devices is None else devices)
        self.states = copy.deepcopy(STATES if states is None else states)
        self.admin = admin
        self.drop_writes = drop_writes
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.ha_version = "2026.10.0"
        self.closed = False
        self.helpers: dict[str, dict[str, dict[str, Any]]] = {
            domain: {item["id"]: copy.deepcopy(item) for item in items}
            for domain, items in (helpers or {}).items()
        }
        self.helper_states: dict[str, str] = {
            f"{domain}.{item_id}": "off"
            for domain, items in self.helpers.items()
            for item_id in items
        }
        self.automations: dict[str, dict[str, Any]] = {
            a["id"]: copy.deepcopy(a) for a in automations or []
        }
        self.yaml_helpers = set(yaml_helpers)
        self.related = copy.deepcopy(related or {})
        self.search_fails = search_fails
        self.yaml_automations = list(yaml_automations)
        self.rejected = set(rejected)
        self.disabled = set(disabled)
        self.lag = lag
        # Automation entities lagging their config: id -> state reads left until they
        # catch up (appear after a write, go after a delete).
        self.appearing: dict[str, int] = {}
        self.lingering: dict[str, int] = {}
        # (automation id, entity_id, old state, new state) for every trigger that matched.
        self.fired: list[tuple[str, str, Any, Any]] = []
        # The event side: handlers per event type, connection listeners, and HA's clock for
        # the states `change` writes.
        self.subscribers: dict[str, list[Any]] = {}
        self.listeners: list[Any] = []
        self.announced = False
        self.clock = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)
        self._contexts = itertools.count(1)

    @property
    def writes(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [call for call in self.calls if call[0] in WRITES]

    def _log(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))

    # Reads
    async def get_states(self) -> list[dict[str, Any]]:
        self._log("get_states")
        states = copy.deepcopy(self.states)
        for entity_id, state in sorted(self.helper_states.items()):
            states.append(_state(entity_id, entity_id.partition(".")[2], state))
        for item_id in sorted(self.yaml_helpers):
            # A YAML helper is an entity like any other, just not in the storage collection.
            states.append(_state(f"input_boolean.{item_id}", item_id))
        hidden = {*self.appearing, *self.disabled}
        visible = [i for i in self.automations if i not in hidden]
        visible += [*self.lingering, *self.yaml_automations]
        for automation_id in sorted(visible):
            state = "unavailable" if automation_id in self.rejected else "on"
            states.append(
                _state(f"automation.{automation_id}", automation_id, state, id=automation_id)
            )
        for pending in (self.appearing, self.lingering):
            for automation_id in list(pending):
                pending[automation_id] -= 1
                if pending[automation_id] <= 0:
                    del pending[automation_id]
        return states

    async def list_areas(self) -> list[dict[str, Any]]:
        self._log("list_areas")
        return copy.deepcopy(self.areas)

    async def list_entity_registry(self) -> list[dict[str, Any]]:
        self._log("list_entity_registry")
        return copy.deepcopy(self.entities)

    async def list_device_registry(self) -> list[dict[str, Any]]:
        self._log("list_device_registry")
        return copy.deepcopy(self.devices)

    async def get_entity_registry_entry(self, entity_id: str) -> dict[str, Any] | None:
        self._log("get_entity_registry_entry", entity_id)
        entry = next((e for e in self.entities if e["entity_id"] == entity_id), None)
        return copy.deepcopy(entry)

    async def list_helpers(self, domain: str) -> list[dict[str, Any]]:
        self._log("list_helpers", domain)
        return copy.deepcopy(list(self.helpers.get(domain, {}).values()))

    async def search_related(self, item_type: str, item_id: str) -> dict[str, list[str]]:
        """Like HA's search: the loaded automations whose config names the entity, plus
        whatever `related` adds for it."""
        self._log("search_related", item_type, item_id)
        if self.search_fails:
            from app.core.ha import HACommandError

            raise HACommandError("unknown_command", "Unknown command.")
        found: dict[str, list[str]] = {}
        hidden = {*self.appearing, *self.disabled}
        for automation_id, automation in sorted(self.automations.items()):
            if automation_id not in hidden and f'"{item_id}"' in json.dumps(automation):
                found.setdefault("automation", []).append(f"automation.{automation_id}")
        for kind, items in self.related.get(item_id, {}).items():
            found.setdefault(kind, []).extend(items)
        return found

    async def get_automation_config(self, automation_id: str) -> dict[str, Any] | None:
        self._log("get_automation_config", automation_id)
        return copy.deepcopy(self.automations.get(automation_id))

    # Writes
    async def create_helper(self, domain: str, data: dict[str, Any]) -> dict[str, Any]:
        self._log("create_helper", domain, copy.deepcopy(data))
        items = self.helpers.setdefault(domain, {})
        base = _slugify(data["name"])
        item_id, n = base, 2
        while item_id in items or item_id in self.yaml_helpers:
            item_id, n = f"{base}_{n}", n + 1
        item = {"id": item_id, **copy.deepcopy(data)}
        if not self.drop_writes:
            items[item_id] = item
            self._set(f"{domain}.{item_id}", "off")
        return copy.deepcopy(item)

    async def update_helper(
        self, domain: str, item_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self._log("update_helper", domain, item_id, copy.deepcopy(data))
        item = {"id": item_id, **copy.deepcopy(data)}
        if not self.drop_writes:
            self.helpers[domain][item_id] = item
        return copy.deepcopy(item)

    async def delete_helper(self, domain: str, item_id: str) -> None:
        self._log("delete_helper", domain, item_id)
        if not self.drop_writes:
            del self.helpers[domain][item_id]
            self._set(f"{domain}.{item_id}", None)

    async def save_automation_config(self, automation_id: str, config: dict[str, Any]) -> None:
        self._log("save_automation_config", automation_id, copy.deepcopy(config))
        if self.drop_writes:
            return
        if automation_id not in self.automations and self.lag:
            self.appearing[automation_id] = self.lag
        self.automations[automation_id] = {"id": automation_id, **copy.deepcopy(config)}

    async def delete_automation_config(self, automation_id: str) -> bool:
        self._log("delete_automation_config", automation_id)
        if automation_id not in self.automations:
            return False
        if not self.drop_writes:
            del self.automations[automation_id]
            if self.lag:
                self.lingering[automation_id] = self.lag
        return True

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        self._log("call_service", domain, service, copy.deepcopy(data))
        if domain == "input_boolean" and not self.drop_writes:
            for entity_id in _as_list(data["entity_id"]):
                old = self.helper_states.get(entity_id)
                new = {"turn_on": "on", "turn_off": "off"}.get(service)
                if service == "toggle":
                    new = "off" if old == "on" else "on"
                self._set(entity_id, new)
        return []

    def _set(self, entity_id: str, new: str | None) -> None:
        """Change an entity's state as HA would, recording every automation it triggers."""
        old = self.helper_states.get(entity_id)
        if new is None:
            self.helper_states.pop(entity_id, None)
        else:
            self.helper_states[entity_id] = new
        self.trigger(entity_id, old, new)

    def trigger(self, entity_id: str, old: Any, new: Any) -> None:
        """Record which loaded automations a state change would trigger."""
        for automation_id, automation in sorted(self.automations.items()):
            if automation_id in self.appearing or automation_id in self.disabled:
                continue
            for trigger in automation.get("triggers", []):
                if state_trigger_matches(trigger, entity_id, old, new):
                    self.fired.append((automation_id, entity_id, old, new))

    def restart(self) -> None:
        """What HA restarting looks like to the automations: every helper restored from no
        state to its last one, every light back from unavailable."""
        for entity_id, state in sorted(self.helper_states.items()):
            self.trigger(entity_id, None, state)
        for state in self.states:
            self.trigger(state["entity_id"], "unavailable", state["state"])

    def set_light(self, entity_id: str, state: str) -> None:
        next(s for s in self.states if s["entity_id"] == entity_id)["state"] = state

    async def set_entity_area(self, entity_id: str, area_id: str | None) -> dict[str, Any]:
        self._log("set_entity_area", entity_id, area_id)
        entry = next(e for e in self.entities if e["entity_id"] == entity_id)
        if self.drop_writes:
            return {**entry, "area_id": area_id}
        entry["area_id"] = area_id
        return copy.deepcopy(entry)

    async def create_area(self, name: str) -> dict[str, Any]:
        self._log("create_area", name)
        base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        area_id, n = base, 2
        while any(a["area_id"] == area_id for a in self.areas):
            area_id, n = f"{base}_{n}", n + 1
        area = {"area_id": area_id, "name": name, "aliases": [], "floor_id": None}
        if not self.drop_writes:
            self.areas.append(area)
        return copy.deepcopy(area)

    async def rename_area(self, area_id: str, name: str) -> dict[str, Any]:
        self._log("rename_area", area_id, name)
        area = next(a for a in self.areas if a["area_id"] == area_id)
        if self.drop_writes:
            return {**area, "name": name}
        area["name"] = name
        return copy.deepcopy(area)

    # The event side (the observer's)
    async def subscribe_events(self, event_type: str, handler: Any) -> None:
        self._log("subscribe_events", event_type)
        handlers = self.subscribers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)
        if not self.announced:
            self.announced = True
            await self.announce("connected", self.ha_version)

    async def unsubscribe_events(self, event_type: str, handler: Any) -> None:
        self._log("unsubscribe_events", event_type)
        handlers = self.subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def add_connection_listener(self, listener: Any) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_connection_listener(self, listener: Any) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def announce(self, state: str, detail: str | None = None) -> None:
        for listener in list(self.listeners):
            await listener(state, detail)

    async def emit(self, event: dict[str, Any]) -> None:
        for handler in list(self.subscribers.get(event["event_type"], [])):
            await handler(copy.deepcopy(event))

    def change(
        self,
        entity_id: str,
        state: str,
        *,
        parent_id: str | None = None,
        user_id: str | None = None,
        context_id: str | None = None,
        **attributes: Any,
    ) -> dict[str, Any]:
        """Set an entity's state as HA would, and return the `state_changed` event HA sends
        for it. `parent_id` marks a change an automation caused through its trigger (the
        bridge, a hard switch); a person at a dimmer has none. `context_id` puts the change
        under a given context, such as an automation run's (`automation_run`)."""
        index = next((i for i, s in enumerate(self.states) if s["entity_id"] == entity_id), None)
        old = copy.deepcopy(self.states[index]) if index is not None else None
        name = (old or {}).get("attributes", {}).get("friendly_name", entity_id)
        new = _state(entity_id, name, state, **attributes)
        self.clock += timedelta(seconds=1)
        new["last_updated"] = self.clock.isoformat()
        new["context"] = {
            "id": context_id or f"ctx{next(self._contexts)}",
            "parent_id": parent_id,
            "user_id": user_id,
        }
        if index is None:
            self.states.append(new)
        else:
            self.states[index] = new
        return {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id, "old_state": old, "new_state": copy.deepcopy(new)},
            "origin": "LOCAL",
            "time_fired": new["last_updated"],
            "context": copy.deepcopy(new["context"]),
        }

    def automation_run(self, automation_id: str) -> tuple[dict[str, Any], str]:
        """The `automation_triggered` event HA fires when an automation with no triggering
        context starts (time, time pattern, sun): a fresh context, no parent. Returns the
        event and the run's context id, which its actions' changes carry."""
        context_id = f"run{next(self._contexts)}"
        event = {
            "event_type": "automation_triggered",
            "data": {"name": automation_id, "entity_id": f"automation.{automation_id}"},
            "context": {"id": context_id, "parent_id": None, "user_id": None},
        }
        return event, context_id

    # The rest of the surface tools/doctor.py uses
    async def ping(self) -> str:
        self._log("ping")
        return "API running."

    async def get_config(self) -> dict[str, Any]:
        self._log("get_config")
        return {"version": self.ha_version, "time_zone": "Europe/London"}

    async def connect(self) -> None:
        self._log("connect")

    async def command(self, message: dict[str, Any]) -> Any:
        self._log("command", message.get("type"))
        if message.get("type") == "auth/current_user":
            return {"id": "user", "name": "Light Map", "is_admin": self.admin}
        raise AssertionError(f"FakeHA has no answer for {message.get('type')}")

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeHA":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
