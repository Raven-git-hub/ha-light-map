"""Switching plane: generate the switching-plane artefacts: per-bulb soft-switch helpers,
hard->soft automations, soft->bulb bridge automations.

Names (the `lm_` lane):

- `input_boolean.lm_soft_<bulb_id>`: the soft switch, one per bulb and only per bulb, and
  none for a bulb the user ignored (P4.1: a light that isn't a room bulb). A
  switching group mints no HA entity of its own: it is a named set of member bulbs plus its
  hard-switch bindings, and a hard switch bound to a group acts on every member's soft
  switch. (A group of input_booleans can't be created over HA's network API, only in
  configuration.yaml, which Light Map never edits; and with no group entity there is no
  group state to keep in sync.)
- `lm_hard_<hard_switch_id>` (hard->soft): one automation per binding. Its trigger comes
  from the binding's `source` and its action from its `mode`:
  - `toggle`: state-aware across the targets: if any target soft is on, turn them all
    off, else turn them all on (an inert HA Jinja condition).
  - `press`: set every target to the binding's `turn` (`on` or `off`) each time it fires:
    a one-way "all off" is a press that turns every target off.
  - `follow`: the targets track the hard switch's own on/off. Needs a `state` source.
- `lm_bridge_<bulb_id>` (soft->bulb): soft on -> `light.turn_on` the bulb; soft off ->
  `light.turn_off`. This is the plain path: the bulb comes back at its last brightness.
  A module may supply the brightness for its bulbs (see "Bridge brightness" below).

Sources (a hard switch's `source`, a tagged union):

- `{type: state, entity_id}`: a stateful switch, binary_sensor, light, or `event.*`
  entity. `follow` acts on its off->on and on->off edges; `toggle` and `press` act on
  every real change of its state (a latching wall switch's flip, an event entity's new
  timestamp), never on a change to or from `unavailable`/`unknown` or the entity
  appearing or disappearing. So a source that comes back from a restart or a reload
  straight to its state is never a press. One that settles through a real change as it
  loads (an HA group whose members come up one by one) does act: bind the physical
  switch's own entity or event, not a group or template built on it.
- `{type: event, event_type, event_data?}`: a stateless button that fires an HA event.
- `{type: device, device_id, trigger}`: a device automation trigger, for a button that
  exposes nothing else. The device id only ever reads a press: it appears in a trigger,
  never in a target.

The bridge never fires on a reload or restart: it triggers only on an `off`->`on` or
`on`->`off` change of its soft switch, so the soft being restored at startup, created,
deleted, or going `unavailable` drives nothing. It runs in `restart` mode and branches on
the soft's state when it runs, so the bulb always ends on the soft's latest state however
fast it is flipped. `core.deploy` sets a soft switch to its bulb's current state before
the bridge goes live, so deploying never drives a bulb.

Bridge brightness (the module seam): the bridge is core, but what brightness a bulb comes
on at is a module's business (Adaptive Light's learned target, read from the almanac at
runtime). A module registers a provider with `register_bridge_brightness`; for each bulb it
returns the `light.turn_on` data to apply on soft-on (`brightness`, `brightness_pct`,
`transition`; values may be HA Jinja, kept inert), or None to leave the bulb on the plain
path. At most one module may claim a bulb. The data lands in the bridge's soft-on branch,
so it is guarded on soft-on by construction and can never power a bulb on by itself; a
module can't retarget the bridge or touch its power. The provider must not bake almanac
values in (it emits a template that reads them at runtime) and must render a valid value
on every path it is applied, since an invalid one fails the whole `light.turn_on`.

A provider may also gate its data (`GatedBrightness`, P5.2a): a condition template under
which the bridge applies it, the plain `light.turn_on` (the bulb's own last brightness)
otherwise. A module doesn't always have a brightness for a bulb it claims (Adaptive Light
has none for a unit with nothing learned, or one held off for the section), and a bridge
that had to render one anyway brought such a bulb up at 255: an off bulb reports no
brightness to keep. The gate only chooses which of two `light.turn_on`s runs, both on the
bridge's own target and both inside the soft-on branch, so the guarantees above hold. The
gate and the data are rendered together, in one `variables` step, before the choice: apart,
a republish landing between the two could leave the data with nothing valid to render for a
gate that had just said yes, and the bulb wouldn't come on. So the data must render without
error even when the gate is false (it is then rendered and not used).

Hard rules:

- The soft switch owns power. The soft->bulb bridge is the only thing that powers a bulb on
  or off. Brightness rides alongside power, always guarded on "soft is on", so a brightness
  command can never power a bulb on.
- Soft switches are `input_boolean`s, pure power toggles: brightness never flows through
  them.
- Entity IDs only, never device IDs, with one carved-out exception: a hard switch's
  `device` source names a device id, and only inside the trigger that reads its press.
  A bulb is addressed by its `entity_id`, never by its `ha_match_key` (an opaque match key,
  compared, never addressed, and never in a generated artefact).
- Output is plain Python data for `core.generate` to serialise: no template engine, HA
  Jinja kept as inert strings, byte-identical output for the same config, and no almanac
  values baked in.
- This lane emits only `lm_*` names and never touches the adaptive module's `al_*`.
- An ignored bulb is in none of it: no soft switch, no bridge, and nothing addresses it
  (bulbs are read through `core.inventory.active_bulbs`). The deploy then removes the
  soft switch and bridge of one that was deployed, as it removes any artefact the config
  no longer calls for, and leaves the light itself as it is.
"""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core import inventory

PREFIX = "lm"
SOFT_DOMAIN = "input_boolean"
SOFT_ICON = "mdi:light-switch"

# What a module may put in the bridge's light.turn_on: how bright, and how fast to get
# there. Never a target, never anything that would make the module, not the soft switch,
# the thing deciding power.
BRIGHTNESS_KEYS = ("brightness", "brightness_pct", "transition")


@dataclass(frozen=True)
class GatedBrightness:
    """A provider's bridge data, applied on soft-on only when `when` (an HA condition
    template, kept inert) renders true; otherwise the bulb takes the plain path and comes
    on at its own last brightness. (`when` None: applied on every soft-on, as a plain
    mapping is.)"""

    when: str | None
    data: Mapping[str, Any]


# (config, room or None for an Unassigned bulb, bulb) -> light.turn_on data (applied on
# every soft-on), gated data, or None (the plain path).
BridgeBrightness = Callable[
    [Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any]],
    Mapping[str, Any] | GatedBrightness | None,
]

# The variables a gated bridge renders its gate and its data into, in one step.
_GATE = "bridge_gate"
_DATA = "bridge_{}"

_BRIDGE_BRIGHTNESS: dict[str, BridgeBrightness] = {}

# A change to or from these is HA losing or regaining an entity, never a press.
_NOT_A_STATE = ("unavailable", "unknown")

_GENERATED = "Generated by Light Map; edits made in Home Assistant are overwritten on deploy."


# -- Names -----------------------------------------------------------------------------


def soft_id(bulb_id: str) -> str:
    """The soft switch helper's object id."""
    return f"{PREFIX}_soft_{bulb_id}"


def soft_entity(bulb_id: str) -> str:
    """The soft switch's entity_id."""
    return f"{SOFT_DOMAIN}.{soft_id(bulb_id)}"


def soft_name(bulb_id: str) -> str:
    """The soft switch helper's name.

    HA gives a helper created over the network the id, and the entity the object id, of
    its slugified name; there is no way to ask for an id. So the name is derived from the
    bulb's id, which is already a slug, never from its display name: "Kitchen strip"
    would become input_boolean.kitchen_strip and every reference would point at nothing.
    (Ported from ha-adaptive-light, which learned it that way.)
    """
    return f"LM Soft {bulb_id}"


def bridge_id(bulb_id: str) -> str:
    return f"{PREFIX}_bridge_{bulb_id}"


def hard_id(hard_switch_id: str) -> str:
    return f"{PREFIX}_hard_{hard_switch_id}"


# -- The bridge-brightness seam --------------------------------------------------------


def register_bridge_brightness(prefix: str, provider: BridgeBrightness) -> None:
    """Let the module with namespace `prefix` supply the brightness its bulbs come on at.

    Called by the composition root (`app.main`), as `core.generate.register_namespace`
    is. See "Bridge brightness" in this module's docstring for the contract.
    """
    if prefix == PREFIX:
        raise ValueError("the switching plane's own bridges take no brightness provider")
    if prefix in _BRIDGE_BRIGHTNESS:
        raise ValueError(f"a bridge-brightness provider is already registered for '{prefix}'")
    _BRIDGE_BRIGHTNESS[prefix] = provider


def bridge_brightness_providers() -> dict[str, BridgeBrightness]:
    """The registered providers, by module prefix."""
    return dict(_BRIDGE_BRIGHTNESS)


def _bridge_brightness(
    config: Mapping[str, Any],
    room: Mapping[str, Any] | None,
    bulb: Mapping[str, Any],
    providers: Mapping[str, BridgeBrightness],
) -> GatedBrightness | None:
    """The one claiming provider's data for a bulb, checked (`when` None when it is applied
    on every soft-on), or None for the plain path."""
    claims = []
    for prefix in sorted(providers):
        data = providers[prefix](config, room, bulb)
        if data is not None:
            claims.append((prefix, data))
    if not claims:
        return None
    if len(claims) > 1:
        raise ValueError(
            f"bulb '{bulb['id']}': more than one module supplies its bridge brightness: "
            f"{[prefix for prefix, _ in claims]}"
        )
    prefix, data = claims[0]
    when = None
    if isinstance(data, GatedBrightness):
        when, data = data.when, data.data
        if when is not None and (not isinstance(when, str) or not when.strip()):
            raise ValueError(f"bulb '{bulb['id']}': '{prefix}' gated its data on {when!r}")
    if not isinstance(data, Mapping) or not data:
        raise ValueError(f"bulb '{bulb['id']}': '{prefix}' supplied no light.turn_on data")
    extra = sorted(set(data) - set(BRIGHTNESS_KEYS))
    if extra:
        raise ValueError(
            f"bulb '{bulb['id']}': '{prefix}' may set only {list(BRIGHTNESS_KEYS)} on the "
            f"bridge, not {extra}: the bridge owns the target and the power"
        )
    for key, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"bulb '{bulb['id']}': '{prefix}' set {key} to {value!r}")
    return GatedBrightness(when, {key: data[key] for key in sorted(data)})


# -- Generation ------------------------------------------------------------------------


def soft_switches(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the soft-switch helpers, one per bulb (none per switching group), as an HA
    package's `input_boolean` section: object id -> fields."""
    return {
        soft_id(bulb["id"]): {"name": soft_name(bulb["id"]), "icon": SOFT_ICON}
        for _, bulb in _bulbs(config)
    }


def hard_switch_automations(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one `lm_hard_<id>` automation per hard-switch binding, sorted by id.

    A binding on a bulb targets that bulb's soft switch; a binding on a switching group
    targets every member's.
    """
    bindings = []
    for _, bulb in _bulbs(config):
        for binding in bulb.get("hard_switches", []):
            what = f"{bulb['name']} ({bulb['entity_id']})"
            bindings.append((binding, [soft_entity(bulb["id"])], what))
    for room in config.get("rooms", []):
        for group in room.get("switching_groups", []):
            targets = sorted(soft_entity(member) for member in group["members"])
            what = f"switching group {group['name']} ({len(targets)} bulbs)"
            for binding in group.get("hard_switches", []):
                bindings.append((binding, targets, what))
    bindings.sort(key=lambda item: item[0]["id"])
    return [_hard_automation(binding, targets, what) for binding, targets, what in bindings]


def bridge_automations(
    config: Mapping[str, Any], providers: Mapping[str, BridgeBrightness] | None = None
) -> list[dict[str, Any]]:
    """Return the `lm_bridge_<id>` soft->bulb automations, one per bulb, sorted by id.

    `providers` defaults to the registered bridge-brightness providers.
    """
    providers = bridge_brightness_providers() if providers is None else providers
    return [
        _bridge(bulb, _bridge_brightness(config, room, bulb, providers))
        for room, bulb in _bulbs(config)
    ]


def generate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return every artefact in the `lm` namespace; the generator registered with
    `core.generate`.

    An HA package: the soft switches, then the automations, bridges before hard switches.
    `core.deploy` writes automations in this order, so no hard switch goes live before
    the bridges of the bulbs it switches.
    """
    return {
        SOFT_DOMAIN: soft_switches(config),
        "automation": [*bridge_automations(config), *hard_switch_automations(config)],
    }


def bridge_mirrors(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """For each bridge, the soft switch and the bulb it joins: `core.deploy` copies the
    bulb's current on/off into the soft before that bridge first goes live."""
    return {
        bridge_id(bulb["id"]): {"helper": soft_entity(bulb["id"]), "entity_id": bulb["entity_id"]}
        for _, bulb in _bulbs(config)
    }


def references(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """What the generated lane addresses in HA, each mapped to a description of who uses
    it: `entities` (every bulb and state source), `devices` (device sources), and the
    subsets `bulbs` and `toggles` (the state sources of toggle bindings). `core.deploy`
    checks them against HA before writing anything."""
    bulbs = {bulb["entity_id"]: f"bulb '{bulb['id']}'" for _, bulb in _bulbs(config)}
    entities: dict[str, str] = dict(bulbs)
    devices: dict[str, str] = {}
    toggles: dict[str, str] = {}
    for binding in _bindings(config):
        source = binding["source"]
        who = f"hard switch '{binding['id']}'"
        if source["type"] == "state":
            entities.setdefault(source["entity_id"], who)
            if binding["mode"] == "toggle":
                toggles.setdefault(source["entity_id"], who)
        elif source["type"] == "device":
            devices.setdefault(source["device_id"], who)
    return {
        name: dict(sorted(found.items()))
        for name, found in (
            ("entities", entities),
            ("devices", devices),
            ("bulbs", bulbs),
            ("toggles", toggles),
        )
    }


def _bulbs(config: Mapping[str, Any]) -> list[tuple[Mapping[str, Any] | None, Mapping[str, Any]]]:
    """Every active (not ignored) bulb with its room (None for Unassigned), sorted by bulb
    id: output follows the bulbs, not the order rooms happen to be listed in. The one place
    this lane reads bulbs, so an ignored one gets no soft switch, bridge, or reference."""
    return sorted(inventory.active_bulbs(config), key=lambda item: item[1]["id"])


def _bindings(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found = [b for _, bulb in _bulbs(config) for b in bulb.get("hard_switches", [])]
    for room in config.get("rooms", []):
        for group in room.get("switching_groups", []):
            found += group.get("hard_switches", [])
    return sorted(found, key=lambda binding: binding["id"])


def _soft_on(light: str, brightness: GatedBrightness | None) -> list[dict[str, Any]]:
    """The bridge's soft-on sequence: the plain `light.turn_on` (the bulb's last brightness),
    or one carrying a module's data, on every soft-on or behind its gate."""
    plain: dict[str, Any] = {"action": "light.turn_on", "target": {"entity_id": light}}
    if brightness is None:
        return [plain]
    if brightness.when is None:
        return [{**plain, "data": dict(brightness.data)}]
    # The gate and every data value rendered in one step, so both read the same states (see
    # "Bridge brightness" above); the choice and the call then use what was rendered.
    variables = {_GATE: brightness.when}
    variables.update({_DATA.format(key): value for key, value in brightness.data.items()})
    data = {key: f"{{{{ {_DATA.format(key)} }}}}" for key in brightness.data}
    return [
        {"variables": variables},
        {
            "if": [{"condition": "template", "value_template": f"{{{{ {_GATE} }}}}"}],
            "then": [{**plain, "data": data}],
            # Nothing from the module for it now: on at its own last brightness, as a bulb
            # no module claims comes on, never at a level the module had to make up.
            "else": [plain],
        },
    ]


def _bridge(bulb: Mapping[str, Any], brightness: GatedBrightness | None) -> dict[str, Any]:
    soft = soft_entity(bulb["id"])
    light = bulb["entity_id"]
    return {
        "id": bridge_id(bulb["id"]),
        "alias": bridge_id(bulb["id"]),
        "description": (
            f"Light Map soft->bulb bridge: {bulb['name']} ({light}) follows {soft}. {_GENERATED}"
        ),
        # restart: a flip that arrives while the last one is still running replaces it,
        # so the bulb ends on the soft's latest state. single would drop it (bulb on, soft
        # off), and parallel could finish the two out of order.
        "mode": "restart",
        # Only real off<->on changes. A soft restored at startup, created, removed, or
        # going unavailable changes from or to something else, so it never fires.
        "triggers": [
            {"trigger": "state", "entity_id": soft, "from": "off", "to": "on"},
            {"trigger": "state", "entity_id": soft, "from": "on", "to": "off"},
        ],
        "conditions": [],
        # Branch on the soft's state when the run starts, not on which trigger fired: the
        # bulb follows the soft, and brightness rides only on the soft-on branch.
        "actions": [
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": soft, "state": "on"}],
                        "sequence": _soft_on(light, brightness),
                    },
                    {
                        "conditions": [{"condition": "state", "entity_id": soft, "state": "off"}],
                        "sequence": [{"action": "light.turn_off", "target": {"entity_id": light}}],
                    },
                ]
            }
        ],
    }


def _hard_automation(
    binding: Mapping[str, Any], targets: Sequence[str], what: str
) -> dict[str, Any]:
    mode = binding["mode"]
    triggers, conditions = _triggers(binding["source"], mode)
    effect = {
        "toggle": f"toggles {what}",
        "press": f"turns {what} {binding.get('turn')}",
        "follow": f"is followed by {what}",
    }[mode]
    return {
        "id": hard_id(binding["id"]),
        "alias": hard_id(binding["id"]),
        "description": f"Light Map hard switch: {binding['name']} {effect}. {_GENERATED}",
        # queued: each press is applied in turn and sees the last one's result, so two
        # quick presses of a toggle are two toggles. single would drop the second, and
        # parallel would let both read the same "before" state.
        "mode": "queued",
        "triggers": triggers,
        "conditions": conditions,
        "actions": _actions(mode, binding.get("turn"), targets),
    }


def _triggers(
    source: Mapping[str, Any], mode: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kind = source["type"]
    if kind == "state":
        entity_id = source["entity_id"]
        if mode == "follow":
            return [
                {"trigger": "state", "entity_id": entity_id, "from": "off", "to": "on", "id": "on"},
                {
                    "trigger": "state",
                    "entity_id": entity_id,
                    "from": "on",
                    "to": "off",
                    "id": "off",
                },
            ], []
        trigger = {
            "trigger": "state",
            "entity_id": entity_id,
            "not_from": list(_NOT_A_STATE),
            "not_to": list(_NOT_A_STATE),
        }
        # not_from/not_to let through a change from or to no state at all: the entity
        # being removed, or re-added when its integration reloads. Neither is a press.
        real_change = {
            "condition": "template",
            "value_template": (
                "{{ trigger.from_state is not none and trigger.to_state is not none }}"
            ),
        }
        return [trigger], [real_change]
    if kind == "event":
        trigger = {"trigger": "event", "event_type": source["event_type"]}
        if source.get("event_data"):
            trigger["event_data"] = _canonical(source["event_data"])
        return [trigger], []
    if kind == "device":
        # The one place a device id reaches HA: reading a button press from a device that
        # exposes no entity or event to bind instead.
        return [
            {"trigger": "device", "device_id": source["device_id"], **_canonical(source["trigger"])}
        ], []
    raise ValueError(f"unknown hard-switch source type {kind!r}")


def _actions(mode: str, turn: str | None, targets: Sequence[str]) -> list[dict[str, Any]]:
    def set_to(state: str) -> dict[str, Any]:
        return {"action": f"input_boolean.turn_{state}", "target": {"entity_id": list(targets)}}

    if mode == "press":
        if turn not in ("on", "off"):
            raise ValueError(f"a press needs turn: on or off, not {turn!r}")
        return [set_to(turn)]
    if mode == "follow":
        return [
            {
                "choose": [
                    {
                        "conditions": [{"condition": "trigger", "id": state}],
                        "sequence": [set_to(state)],
                    }
                    for state in ("on", "off")
                ]
            }
        ]
    if mode == "toggle":
        # State-aware across the targets: one press turns a half-lit group off rather than
        # flipping each member, which would swap which ones are lit. HA Jinja, built as a
        # plain string and never rendered here.
        any_on = " or ".join(f"is_state('{target}', 'on')" for target in targets)
        return [
            {
                "if": [{"condition": "template", "value_template": f"{{{{ {any_on} }}}}"}],
                "then": [set_to("off")],
                "else": [set_to("on")],
            }
        ]
    raise ValueError(f"unknown hard-switch mode {mode!r}")


def _canonical(value: Any) -> Any:
    """A deep copy with every mapping's keys sorted: user-supplied event data and device
    triggers serialise the same whatever order the config lists their keys in."""
    if isinstance(value, Mapping):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return copy.deepcopy(value)
