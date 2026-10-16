"""Generation: namespace registry + deterministic YAML assembly. NO template engine: assemble
Python data structures, serialise with PyYAML; HA Jinja stays an inert string. Same config
-> byte-identical output.

Each namespace (`lm` for the switching plane, `al` for Adaptive Light) registers one
generator; `core.deploy` reconciles each namespace in its own lane. The core registers its
own `lm` lane here; a module's lane is registered by the composition root (`app.main`),
which is how `al` arrives in P5.

A generator returns its artefacts as an HA package: helper domains (e.g. `input_boolean`)
map object id -> fields, as in HA's YAML; `automation` is a list of automation configs, as
in automations.yaml. Every artefact id must be in the generator's own lane: `<prefix>_...`,
and not inside a longer registered prefix (were the adaptive lane ever folded under
`lm_al_`, `lm` would still never claim it).

Hard rules:

- No template engine. Generated automations embed HA Jinja verbatim. Rendering Jinja to
  produce Jinja makes the outer pass consume the inner one.
- Deterministic generation: the same config must always produce byte-identical YAML. Don't
  let dict/set ordering, timestamps, or other nondeterminism leak into output.
- Nothing from the almanac is hardcoded into generated YAML. Automations read the almanac at
  runtime and branch, so flipping a unit to "off" for a section needs only a republish, not
  regeneration.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

import yaml

from app.core import switching

NamespaceGenerator = Callable[[Mapping[str, Any]], dict[str, Any]]

AUTOMATION = "automation"

_PREFIX = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# Registration order is the deploy order: the core's `lm` first (a module's automations
# command its soft switches, so those must exist first), then modules as the composition
# root registers them. Dicts keep insertion order, so this is stable.
_GENERATORS: dict[str, NamespaceGenerator] = {}


def register_namespace(prefix: str, generator: NamespaceGenerator) -> None:
    """Reserve an artefact prefix for a generator."""
    if not _PREFIX.match(prefix):
        raise ValueError(f"not a namespace prefix: {prefix!r}")
    if prefix in _GENERATORS:
        raise ValueError(f"namespace '{prefix}' is already registered")
    _GENERATORS[prefix] = generator


def namespaces() -> list[str]:
    """Return the registered prefixes in a stable order: registration order, the core's
    `lm` first."""
    return list(_GENERATORS)


def in_lane(name: str, prefix: str) -> bool:
    """True if an artefact id (or object id) belongs to namespace `prefix`'s lane.

    A name is in a lane when it starts with `<prefix>_` and isn't claimed by a longer
    registered prefix nested inside it.
    """
    if not name.startswith(f"{prefix}_"):
        return False
    return not any(
        len(other) > len(prefix) and name.startswith(f"{other}_") for other in _GENERATORS
    )


def generate(config: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    """Return the artefacts of one namespace for the config.

    Raises ValueError for an unregistered prefix, or if the generator emitted anything
    outside its own lane: the lane boundary is checked here, at the seam, so no module
    can mint names in another's namespace.
    """
    try:
        generator = _GENERATORS[prefix]
    except KeyError:
        raise ValueError(f"no generator is registered for namespace '{prefix}'") from None
    artefacts = generator(config)
    stray = [name for name in artefact_ids(artefacts) if not in_lane(name, prefix)]
    if stray:
        raise ValueError(f"namespace '{prefix}' generated artefacts outside its lane: {stray}")
    return artefacts


def artefact_ids(artefacts: Mapping[str, Any]) -> list[str]:
    """Every artefact id in an HA package: helper object ids and automation ids."""
    ids: list[str] = []
    for kind, items in artefacts.items():
        if kind == AUTOMATION:
            ids += [str(item.get("id")) for item in items]
        else:
            ids += [str(item_id) for item_id in items]
    return ids


# -- YAML ------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Ported from ha-adaptive-light's generator: no anchors or aliases, block scalars for
    multi-line strings, HA Jinja double-quoted, sequences indented under their key."""

    def ignore_aliases(self, data: Any) -> bool:
        # PyYAML emits `&id001` / `*id001` whenever one object appears twice in the
        # structure (a target list reused by two branches, say). The YAML still parses,
        # but the output then depends on object identity, not just on values.
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        # Indent sequences under their key, as HA writes its own YAML.
        return super().increase_indent(flow, False)


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        style: str | None = "|"
    elif "{{" in data or "{%" in data:
        # Plain style is impossible for a string starting with "{", and PyYAML's default
        # single quotes would double every quote inside the Jinja.
        style = '"'
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _refuse_set(dumper: yaml.SafeDumper, data: Any) -> yaml.Node:
    # A set iterates in hash order, which varies between runs: serialising one would make
    # the output nondeterministic. Generators must emit sorted lists instead.
    raise yaml.representer.RepresenterError(f"refusing to serialise a set: {data!r}")


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(set, _refuse_set)
_Dumper.add_representer(frozenset, _refuse_set)


def to_yaml(data: Any) -> str:
    """Serialise assembled artefacts with PyYAML, deterministically, with HA Jinja passed
    through verbatim."""
    # sort_keys=False: generators build every mapping in a fixed order, which reads
    # better (id, alias, triggers, ... as HA writes them) than alphabetical. A wide width
    # keeps a long Jinja expression on one line rather than folded across several: it
    # would parse back the same, but no longer reads as the template HA runs.
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


register_namespace(switching.PREFIX, switching.generate)
