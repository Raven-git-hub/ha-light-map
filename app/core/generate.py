"""Generation: namespace registry + deterministic YAML assembly. NO template engine: assemble
Python data structures, serialise with PyYAML; HA Jinja stays an inert string. Same config
-> byte-identical output.

Each namespace (`lm` for the switching plane, `al` for Adaptive Light) registers one
generator; `core.deploy` reconciles each namespace in its own lane.

Hard rules:

- No template engine. Generated automations embed HA Jinja verbatim. Rendering Jinja to
  produce Jinja makes the outer pass consume the inner one.
- Deterministic generation: the same config must always produce byte-identical YAML. Don't
  let dict/set ordering, timestamps, or other nondeterminism leak into output.
- Nothing from the almanac is hardcoded into generated YAML. Automations read the almanac at
  runtime and branch, so flipping a unit to "off" for a section needs only a republish, not
  regeneration.
"""

from collections.abc import Callable, Mapping
from typing import Any

NamespaceGenerator = Callable[[Mapping[str, Any]], dict[str, Any]]


def register_namespace(prefix: str, generator: NamespaceGenerator) -> None:
    """Reserve an artefact prefix for a generator."""
    raise NotImplementedError


def namespaces() -> list[str]:
    """Return the registered prefixes in a stable order."""
    raise NotImplementedError


def generate(config: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    """Return the artefacts of one namespace for the config."""
    raise NotImplementedError


def to_yaml(data: Any) -> str:
    """Serialise assembled artefacts with PyYAML, deterministically, with HA Jinja passed
    through verbatim."""
    raise NotImplementedError
