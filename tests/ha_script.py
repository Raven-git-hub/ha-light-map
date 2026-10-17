"""A small stand-in for Home Assistant's template and script engine, for tests only: enough
to render the Jinja the generators emit and run the automations they build against a fake
state machine, so a test can see what a scene, a maintenance tick or a bridge would do.

Never used by the app (which has no template engine: its Jinja is inert data). It needs
jinja2, a test-only dependency (requirements-dev.txt): `AVAILABLE` is False without it, and
the tests that run templates skip.

What it copies from HA, closely, because the generated templates depend on it:

- `states(entity_id)` ('unknown' for an entity HA doesn't have), `state_attr`, `is_state`,
  as functions and as filters.
- The `int` and `float` filters are HA's forgiving ones: with a default, anything that
  doesn't convert gives the default; without one, it is a template error.
- Results are parsed to native types as HA does (`literal_eval`, keeping the text when it
  doesn't parse or parses to a string, and a number only when the text is a plain decimal):
  a `variables` value or service data rendering "128" is the int 128, "{'a': 1}" a dict,
  "none" the string "none", and "1e-05" the string "1e-05".
- A template condition is true only when it renders "true" (any case), as HA's is.
- `light.turn_on` validates its data as HA's schema does: `brightness` must coerce to an
  int (HA clamps it to 0..255) and a brightness of 0 turns the light off; a data value
  that doesn't coerce fails the whole call (`ServiceError`). With no brightness, an off
  light comes back at the brightness it had when it went off (`memory`; a bulb that never
  had one comes on full), as HA's lights do.
- A state change runs, at once, every automation whose state trigger matches it (the
  bridge a soft switch flips), with `tests.fakes.state_trigger_matches`.

Not copied: time (a delay is recorded, not waited), run modes and concurrency (a test that
needs another run in progress sets that automation's `current` attribute), restarts, and
anything the generated automations don't use.
"""

import ast
import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any

from tests.fakes import state_trigger_matches

try:
    import jinja2
except ImportError:  # pragma: no cover - the template tests skip
    jinja2 = None

AVAILABLE = jinja2 is not None

_NO_DEFAULT = object()

# HA's own (homeassistant.helpers.template): a number is kept only when its text reads as a
# plain decimal, so "1e-05", "nan" and "inf" stay strings.
_IS_NUMERIC = re.compile(r"^[+-]?(?!0\d)\d*(?:\.\d*)?$")


class TemplateError(Exception):
    """A template HA would refuse to render."""


class ServiceError(Exception):
    """A service call HA would refuse (its data failed the service's schema)."""


def _forgiving_int(value: Any, default: Any = _NO_DEFAULT, base: int = 10) -> Any:
    try:
        return int(value, base) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            if default is _NO_DEFAULT:
                raise TemplateError(f"int got invalid input {value!r}") from None
            return default


def _forgiving_float(value: Any, default: Any = _NO_DEFAULT) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is _NO_DEFAULT:
            raise TemplateError(f"float got invalid input {value!r}") from None
        return default


def native(text: str) -> Any:
    """HA's result parsing: a literal when the text is one (and isn't a string, or a number
    written any way but as a plain decimal), else the text itself."""
    try:
        result = ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return text
    if isinstance(result, (str, complex)):
        return text
    if (
        isinstance(result, (int, float))
        and not isinstance(result, bool)
        and not _IS_NUMERIC.match(text)
    ):
        return text
    return result


class HA:
    """A state machine (`states`: entity_id -> {state, attributes}), the automations that
    react to it, and a record of every service call (`calls`: (service, entity_id, data,
    by), `by` being the automation that made it)."""

    def __init__(
        self,
        states: Mapping[str, Any] | None = None,
        automations: Sequence[Mapping[str, Any]] = (),
        options: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if not AVAILABLE:
            raise RuntimeError("jinja2 is not installed")
        self.states: dict[str, dict[str, Any]] = {}
        for entity_id, value in (states or {}).items():
            self.set(entity_id, *(value if isinstance(value, tuple) else (value,)))
        self.automations = [copy.deepcopy(dict(a)) for a in automations]
        self.options = {k: list(v) for k, v in (options or {}).items()}
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []
        # Each light's brightness when it last went off, which a plain turn_on restores.
        self.memory: dict[str, int] = {}
        self.delays: list[Any] = []
        self.notifications: list[dict[str, Any]] = []
        self.env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.env.filters["int"] = _forgiving_int
        self.env.filters["float"] = _forgiving_float
        # HA offers these as filters as well as functions (`map('states')`).
        for name, function in (
            ("states", self._state_of),
            ("state_attr", self._attr_of),
            ("is_state", self._is_state),
        ):
            self.env.globals[name] = function
            self.env.filters[name] = function

    # The state machine
    def set(self, entity_id: str, state: str | None, attributes: Mapping[str, Any] | None = None):
        if state is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = {"state": state, "attributes": dict(attributes or {})}

    def state(self, entity_id: str) -> str | None:
        return (self.states.get(entity_id) or {}).get("state")

    def attr(self, entity_id: str, name: str) -> Any:
        return ((self.states.get(entity_id) or {}).get("attributes") or {}).get(name)

    def _state_of(self, entity_id: str) -> str:
        return self.state(entity_id) or "unknown"

    def _attr_of(self, entity_id: str, name: str) -> Any:
        return copy.deepcopy(self.attr(entity_id, name))

    def _is_state(self, entity_id: str, state: str) -> bool:
        return self.state(entity_id) == state

    # Templates
    def render(self, text: Any, variables: Mapping[str, Any] | None = None) -> Any:
        """A value as HA renders it: a template string rendered and parsed to a native
        type; anything else as it is (nested data rendered throughout)."""
        if isinstance(text, Mapping):
            return {k: self.render(v, variables) for k, v in text.items()}
        if isinstance(text, list):
            return [self.render(v, variables) for v in text]
        if not isinstance(text, str) or ("{{" not in text and "{%" not in text):
            return text
        try:
            rendered = self.env.from_string(text).render(**dict(variables or {}))
        except jinja2.TemplateError as exc:
            raise TemplateError(f"{exc} in {text!r}") from exc
        return native(rendered)

    def condition(self, cond: Mapping[str, Any], variables: Mapping[str, Any]) -> bool:
        kind = cond["condition"]
        if kind == "state":
            return self.state(cond["entity_id"]) == cond["state"]
        if kind == "template":
            text = self.env.from_string(cond["value_template"]).render(**dict(variables))
            return text.strip().lower() == "true"
        if kind == "trigger":
            return variables.get("trigger", {}).get("id") == cond["id"]
        raise AssertionError(f"condition {kind!r} isn't modelled")

    # Running
    def run(
        self,
        automation: Mapping[str, Any],
        *,
        check_conditions: bool = True,
        trigger: Mapping[str, Any] | None = None,
    ) -> bool:
        """Run an automation as HA would on a trigger (`check_conditions`; `trigger` is
        what fired it, as the `trigger` variable) or as `automation.trigger` does (skipping
        them). Returns whether its actions ran to the end (False if a condition stopped
        them)."""
        variables: dict[str, Any] = {"trigger": dict(trigger or {})}
        if check_conditions and not all(
            self.condition(c, variables) for c in automation.get("conditions", [])
        ):
            return False
        return self._sequence(automation["actions"], variables, automation["id"])

    def _sequence(self, actions: Sequence[Mapping[str, Any]], variables: dict, by: str) -> bool:
        for action in actions:
            if not self._step(action, variables, by):
                return False
        return True

    def _step(self, action: Mapping[str, Any], variables: dict, by: str) -> bool:
        if "variables" in action:
            for name, value in action["variables"].items():
                variables[name] = self.render(value, variables)
            return True
        if "condition" in action:
            return self.condition(action, variables)
        if "if" in action:
            branch = "then" if all(self.condition(c, variables) for c in action["if"]) else "else"
            return self._sequence(action.get(branch, []), variables, by)
        if "choose" in action:
            for option in action["choose"]:
                if all(self.condition(c, variables) for c in option["conditions"]):
                    return self._sequence(option["sequence"], variables, by)
            return self._sequence(action.get("default", []), variables, by)
        if "delay" in action:
            self.delays.append(action["delay"])
            return True
        if "action" in action:
            self._call(action, variables, by)
            return True
        raise AssertionError(f"action {action!r} isn't modelled")

    def _call(self, action: Mapping[str, Any], variables: dict, by: str) -> None:
        service = action["action"]
        data = self.render(dict(action.get("data", {})), variables)
        targets = (action.get("target") or {}).get("entity_id", [])
        targets = targets if isinstance(targets, list) else [targets]
        if service == "persistent_notification.create":
            self.notifications.append(data)
            self.calls.append((service, "", data, by))
            return
        for entity_id in targets:
            self.calls.append((service, entity_id, data, by))
            self._apply(service, entity_id, data)

    def _apply(self, service: str, entity_id: str, data: Mapping[str, Any]) -> None:
        domain, _, verb = service.partition(".")
        if domain == "input_boolean":
            self._change(entity_id, {"turn_on": "on", "turn_off": "off"}[verb])
        elif service == "input_select.select_option":
            options = self.options.get(entity_id)
            if options is not None and data["option"] not in options:
                raise ServiceError(f"{data['option']!r} is not an option of {entity_id}")
            self._change(entity_id, data["option"])
        elif service == "light.turn_on":
            attributes = dict((self.states.get(entity_id) or {}).get("attributes") or {})
            for key in ("brightness", "transition"):
                if key in data:
                    try:
                        value = int(data[key]) if key == "brightness" else float(data[key])
                    except (TypeError, ValueError):
                        raise ServiceError(f"{service}: {key} {data[key]!r} is invalid") from None
                    data = {**data, key: value}
            if "brightness" in data:
                if min(max(data["brightness"], 0), 255) == 0:
                    self._light_off(entity_id, attributes)
                    return
                attributes["brightness"] = min(data["brightness"], 255)
            elif attributes.get("brightness") is None:
                # Back at its level when it went off; a bulb with no memory comes on full.
                attributes["brightness"] = self.memory.get(entity_id, 255)
            self._change(entity_id, "on", attributes)
        elif service == "light.turn_off":
            attributes = dict((self.states.get(entity_id) or {}).get("attributes") or {})
            self._light_off(entity_id, attributes)
        else:
            raise AssertionError(f"service {service} isn't modelled")

    def _light_off(self, entity_id: str, attributes: dict[str, Any]) -> None:
        if attributes.get("brightness"):
            self.memory[entity_id] = attributes["brightness"]
        attributes["brightness"] = None
        self._change(entity_id, "off", attributes)

    def _change(self, entity_id: str, new: str, attributes: Mapping[str, Any] | None = None):
        old = self.state(entity_id)
        if attributes is None:
            attributes = (self.states.get(entity_id) or {}).get("attributes")
        self.set(entity_id, new, attributes)
        if old == new:
            return
        for automation in self.automations:
            if any(state_trigger_matches(t, entity_id, old, new) for t in automation["triggers"]):
                self.run(automation)

    # Reading the record
    def services(self, by: str | None = None) -> list[tuple[str, str]]:
        """(service, entity_id) of each call, optionally only those `by` one automation."""
        return [(s, e) for s, e, _, who in self.calls if by is None or who == by]
