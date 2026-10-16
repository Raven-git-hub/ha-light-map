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
- sensor.living_room_lux: not a light.
"""

import copy
import re
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


def _entity(entity_id: str, device_id: str | None, area_id: str | None) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "id": f"reg_{entity_id.replace('.', '_')}",
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


STATES: list[dict[str, Any]] = [
    _state("light.chandelier_1", "Chandelier 1", "on", brightness=128),
    _state("light.chandelier_2", "Chandelier 2", "on", brightness=128),
    _state("light.chandelier_3", "Chandelier 3", "unavailable"),
    _state("light.reading_lamp", "Reading lamp"),
    _state("light.counter_lamp", "Counter lamp"),
    _state("light.kitchen_strip", "Kitchen strip"),
    _state("light.porch", "Porch"),
    _state("light.legacy_yaml", "Legacy YAML light"),
    _state(
        "light.living_room_group",
        "Living room lights",
        entity_id=["light.chandelier_1", "light.chandelier_2", "light.chandelier_3"],
    ),
    _state("sensor.living_room_lux", "Living room lux", "42", device_class="illuminance"),
]


def light(
    entity_id: str, area_id: str | None, name: str | None = None, *, is_group: bool = False
) -> dict[str, Any]:
    """A discovered-light record, as `inventory.discover_lights` returns it."""
    return {
        "entity_id": entity_id,
        "name": name or entity_id.partition(".")[2].replace("_", " ").capitalize(),
        "state": "off",
        "area_id": area_id,
        "area_inherited": False,
        "in_registry": True,
        "is_group": is_group,
    }


def bulb(bulb_id: str, entity_id: str | None = None, name: str | None = None, **extra: Any):
    return {
        "id": bulb_id,
        "name": name or bulb_id.replace("_", " ").capitalize(),
        "entity_id": entity_id or f"light.{bulb_id}",
        "hard_switches": [],
        **extra,
    }


def committed_config() -> dict[str, Any]:
    """A valid committed config for the fixture house, as of some earlier sync."""
    return {
        "version": 1,
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
                                "source": "switch.reading_relay",
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
                                "source": "event.living_room_button",
                                "mode": "press",
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
                    "learning_units": [
                        {
                            "id": "chandelier",
                            "name": "Chandelier",
                            "members": ["chandelier_1", "chandelier_2", "chandelier_3"],
                        },
                        {"id": "reading", "name": "Reading", "members": ["reading_lamp"]},
                    ],
                    "scenes": {},
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


WRITES = ("set_entity_area", "create_area", "rename_area")


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
    ) -> None:
        self.areas = copy.deepcopy(AREAS if areas is None else areas)
        self.entities = copy.deepcopy(ENTITIES if entities is None else entities)
        self.devices = copy.deepcopy(DEVICES if devices is None else devices)
        self.states = copy.deepcopy(STATES if states is None else states)
        self.admin = admin
        self.drop_writes = drop_writes
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.ha_version = "2026.10.0"
        self.closed = False

    @property
    def writes(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [call for call in self.calls if call[0] in WRITES]

    def _log(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))

    # Reads
    async def get_states(self) -> list[dict[str, Any]]:
        self._log("get_states")
        return copy.deepcopy(self.states)

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

    # Writes
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
