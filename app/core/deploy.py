"""Deploy: reconcile helpers/automations/Area writes against live HA across namespaces;
create/update/remove; verify after write. Each namespace stays in its own lane.

One namespace at a time: `fetch_live` reads what HA holds in the lane, `plan` diffs it
against what the generator wants, `apply` carries the plan out, and `verify` re-reads HA
until everything matches. `deploy` runs the four. Helpers are UI-managed (storage
collection) helpers, written over the WebSocket; automations live in automations.yaml,
written through HA's config API. A failed or interrupted deploy is reconciled by the next
one, which plans from whatever HA then holds.

A lane owns the helper domains its generated package names (the switching plane's
`input_boolean`; the adaptive lane's `input_boolean` and `input_select`, P5.1): a deploy
reads, plans, verifies and summarises only those, so adding a domain for one lane changes
nothing another lane reads or writes. Before writing, a lane's registered references
(`core.generate.lane_references`: every bulb, source, soft switch or sensor its artefacts
address) must all exist in HA.

The order of a plan's steps is what keeps a deploy from driving anything:

1. Helpers are created and updated first, so no automation references a missing one.
2. Before a bridge first goes live, its soft switch is set to its bulb's current on/off
   (`sync`). The bridge doesn't exist yet, so nothing follows the change; once it does,
   soft and bulb already agree, and it fires only when something next flips the soft.
   A bulb that is unavailable leaves its soft as it is (a new one starts off).
3. Automations are written in the generator's order: for `lm`, bridges before hard
   switches, so no hard switch goes live before the bridges of the bulbs it switches.
4. Automations are removed before the helpers they reference.

Nothing in a deploy calls a light service, and removing a bulb's artefacts never turns it
off: a bridge triggers only on its soft switch changing between `off` and `on`, which a
deleted or (re)created helper never does.

Area writes (`deploy_areas`) are separate: they make HA's Areas match the committed room
snapshot, through `core.inventory`'s own verified moves, and are refused before anything
is written when the snapshot is too stale to apply. An ignored bulb's light is never
moved: Light Map has set it aside, so whatever Area HA gives it stands.

`crossover` is the read-only report a deploy preview shows beside the plan: what else in
HA already drives the bulbs Light Map is about to bridge (foreign automations, scripts,
scenes, and groups, the live ha-adaptive-light's `al_` automations among them), and the
names Light Map would generate that something else in HA already holds.

Hard rules:

- Separate lanes: the switching deployer reconciles `lm_*` / `lm_soft_*` and never touches
  `al_*`, and vice versa. A plan for one namespace never creates, updates, or removes
  anything outside it.
- Every write is verified after the fact.
- Area/registry writes are a new write surface: verified, reconcilable, never destructive by
  surprise.
- Entity IDs only, never device IDs. A bulb's `ha_match_key` is an opaque match key: an
  Area write compares it with the registry entry at the bulb's entity_id (so it never moves
  a different light), and nothing addresses it. A hard switch's device source is read only
  to check the device still exists.
- During the parallel build (P0-P5) Light Map runs beside the live ha-adaptive-light, so
  only new artefacts are deployed and the new bridges stay unbound (no hard switches; bridge
  not yet authoritative) until cutover (P6). Binding a hard switch in the config is the
  step that makes a bridge authoritative; deploying alone never drives a bulb. The `al_`
  lane holds the live system's own names, so it isn't even registered until cutover (the
  composition root's cutover switch); nothing here deploys a lane that isn't registered.
"""

import asyncio
import copy
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.core import config as config_mod
from app.core import generate as generate_mod
from app.core import inventory, switching
from app.core.generate import AUTOMATION, in_lane
from app.core.ha import HAClient, HAError
from app.core.inventory import InventoryError

log = logging.getLogger(__name__)

# The helper domains a lane may own. Each is a UI-managed storage collection with the same
# list/create/update/delete commands; a module needing another adds it here. A lane owns
# only the domains its package names (see `lane_domains`): input_select is the adaptive
# lane's scene select (P5.1), and the switching lane never reads or writes one.
HELPER_DOMAINS = ("input_boolean", "input_select")

# HA reloads a written automation in the background, so its entity can lag the write.
VERIFY_ATTEMPTS = 5
VERIFY_DELAY = 1.0

# The automation ids HA's config API can address in a URL path (see `core.ha`).
_ADDRESSABLE = re.compile(r"^[a-z0-9_]+$")


class DeployError(Exception):
    """A deploy was refused before writing, a write was not taken as asked, or HA did not
    hold what was written when re-read. Carries every problem found.

    `written` is None for a refusal before anything was written; once writing has begun it
    lists what was written (for `deploy_areas`, the moves that landed), so a caller never
    reports a half-done write as a refusal.
    """

    def __init__(
        self, problems: str | Sequence[str], *, written: Sequence[Any] | None = None
    ) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        self.written = None if written is None else list(written)
        super().__init__("; ".join(self.problems))


class StalePreview(DeployError):
    """HA no longer matches the preview the caller was shown; nothing was written."""


# -- Reading ---------------------------------------------------------------------------


def lane_domains(desired: Mapping[str, Any]) -> list[str]:
    """The helper domains a lane owns: those its generated package names (empty or not),
    in HELPER_DOMAINS order. Reading only these keeps a lane from listing, and so from
    ever planning a removal in, a domain it has never written: when input_select joined
    HELPER_DOMAINS for the adaptive lane, the switching lane's reads and writes stayed
    exactly what they were (and an HA without the input_select integration loaded, whose
    `input_select/list` fails, still deploys the switching plane)."""
    return [domain for domain in HELPER_DOMAINS if domain in desired]


async def fetch_live(
    ha: HAClient,
    prefix: str,
    *,
    expected: Iterable[str] = (),
    domains: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return the artefacts currently in HA under one namespace prefix.

    `domains` are the helper domains to read (the lane's own, `lane_domains`; default all
    of HELPER_DOMAINS). Each maps object id -> the stored fields (the item without its `id`);
    `automation` maps id -> stored config; `unloaded` lists the automations whose config
    is stored but which HA hasn't loaded (no entity, or an `unavailable` one); `unmanaged`
    lists the lane's automations that HA's config API can't see (defined in YAML outside
    automations.yaml, or under an id the API can't address), which a deploy can neither
    update nor remove; `held` lists the lane's helper entity ids that HA has as entities
    but not as UI-managed helpers (YAML ones), which a deploy can't create.

    HA lists automations only as entities, so one with no entity is found only if its id
    is in `expected`: pass the ids the deploy wants, so that an automation whose entity is
    disabled, or still reloading after an interrupted deploy, reads as there, not as
    missing. (Taken for missing, its bridge would be "created" again, with its soft switch
    re-synced to the bulb while the bridge might already be live.)
    """
    states = await ha.get_states()
    domains = HELPER_DOMAINS if domains is None else tuple(domains)
    live: dict[str, Any] = {domain: await _helpers(ha, domain, prefix) for domain in domains}
    entities = _automation_entities(states, prefix)
    automations: dict[str, Any] = {}
    unmanaged: list[str] = []
    for automation_id in sorted(set(entities) | {i for i in expected if in_lane(i, prefix)}):
        stored = None
        if _ADDRESSABLE.match(automation_id):
            stored = await ha.get_automation_config(automation_id)
        if stored is not None:
            automations[automation_id] = stored
        elif automation_id in entities:
            unmanaged.append(automation_id)
    live[AUTOMATION] = automations
    live["unloaded"] = [i for i in automations if entities.get(i, "unavailable") == "unavailable"]
    live["unmanaged"] = unmanaged
    live["held"] = _held(states, live, prefix, domains)
    return live


def _held(
    states: Sequence[Mapping[str, Any]],
    live: Mapping[str, Any],
    prefix: str,
    domains: Sequence[str],
) -> list[str]:
    """The lane's helper entity ids (in the domains read) that HA has as entities but not
    as UI-managed helpers (a YAML input_boolean, say). Creating a helper under one of those
    ids would land it under `<id>_2` instead, with every generated reference pointing at
    the other one."""
    held = []
    for state in states:
        domain, _, object_id = str(state.get("entity_id", "")).partition(".")
        known = live.get(domain, {})
        if domain in domains and in_lane(object_id, prefix) and object_id not in known:
            held.append(f"{domain}.{object_id}")
    return sorted(held)


async def _helpers(ha: HAClient, domain: str, prefix: str) -> dict[str, dict[str, Any]]:
    items = [i for i in await ha.list_helpers(domain) if isinstance(i.get("id"), str)]
    return {
        item["id"]: _fields(item)
        for item in sorted(items, key=lambda i: i["id"])
        if in_lane(item["id"], prefix)
    }


def _fields(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "id"}


def _automation_entities(states: Sequence[Mapping[str, Any]], prefix: str) -> dict[str, str]:
    """The lane's automation entities: config id -> entity state. HA lists automations
    only as entities, each carrying its config `id` as an attribute."""
    found: dict[str, str] = {}
    for state in states:
        if not str(state.get("entity_id", "")).startswith("automation."):
            continue
        automation_id = (state.get("attributes") or {}).get("id")
        if isinstance(automation_id, str) and in_lane(automation_id, prefix):
            found[automation_id] = str(state.get("state"))
    return found


# -- Planning --------------------------------------------------------------------------


def plan(
    prefix: str,
    desired: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    mirrors: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Diff desired against live artefacts in one namespace: what to create, update,
    remove.

    `desired` is the namespace's generated package, `live` what `fetch_live` returned.
    `mirrors` maps an automation id to `{helper, entity_id}`: before that automation is
    first created, the helper (an input_boolean) is set to the entity's current on/off.
    An automation that is stored but not loaded is written again (HA reloads it), never
    created, so its helper is not synced again.

    Returns `{prefix, desired, steps, warnings}`, `steps` in the order `apply` runs them
    (see the module docstring). Pure: nothing is read or written. Anything in `live`
    outside the lane, or in a helper domain `desired` doesn't name, is ignored, so it can
    never be updated or removed. Raises
    DeployError, before anything is written, when a desired artefact can't be deployed as
    named.
    """
    mirrors = mirrors or {}
    stray = [n for n in generate_mod.artefact_ids(desired) if not in_lane(n, prefix)]
    if stray:
        raise ValueError(f"a plan for '{prefix}' may not include {stray}")
    unknown = sorted(set(desired) - {AUTOMATION, *HELPER_DOMAINS})
    if unknown:
        raise ValueError(f"deploy can't write {unknown}")

    problems: list[str] = []
    helper_writes: list[dict[str, Any]] = []
    helper_removes: list[dict[str, Any]] = []
    held = set(live.get("held", []))
    for domain in lane_domains(desired):
        want: Mapping[str, Any] = desired[domain]
        have = {k: v for k, v in live.get(domain, {}).items() if in_lane(k, prefix)}
        for item_id, fields in want.items():
            # HA names a new helper by slugifying its name; ask for anything else and the
            # entity lands under another id, with every generated reference pointing at
            # nothing.
            slug = _slugify(str(fields.get("name", "")))
            if slug != item_id:
                problems.append(
                    f"{domain}.{item_id}: its name {fields.get('name')!r} would make HA create "
                    f"{domain}.{slug}"
                )
            if item_id not in have:
                if f"{domain}.{item_id}" in held:
                    # Caught here, before anything is written: found only after the create
                    # (see `apply`), it would leave a stray `<id>_2` helper behind.
                    problems.append(
                        f"{domain}.{item_id} already exists in HA but is not one of Light "
                        "Map's UI-managed helpers (a YAML helper, or another helper's entity "
                        f"renamed to it?), so HA would create {domain}.{item_id}_2: free the "
                        "id there"
                    )
                helper_writes.append(_step("create", domain, item_id, fields))
            elif dict(have[item_id]) != dict(fields):
                helper_writes.append(_step("update", domain, item_id, fields))
        helper_removes += [_step("remove", domain, i) for i in sorted(set(have) - set(want))]

    wanted = list(desired.get(AUTOMATION, []))
    wanted_ids = {automation["id"] for automation in wanted}
    have_automations = {k: v for k, v in live.get(AUTOMATION, {}).items() if in_lane(k, prefix)}
    unmanaged = [i for i in live.get("unmanaged", []) if in_lane(i, prefix)]
    unloaded = set(live.get("unloaded", []))
    syncs: list[dict[str, Any]] = []
    automation_writes: list[dict[str, Any]] = []
    for automation in wanted:
        automation_id = automation["id"]
        if automation_id in unmanaged:
            problems.append(
                f"automation {automation_id} is already defined in HA's YAML outside "
                "automations.yaml, where Light Map can't manage it: remove it there"
            )
        if automation_id not in have_automations:
            mirror = mirrors.get(automation_id)
            if mirror is not None:
                syncs.append(_sync_step(prefix, desired, automation_id, mirror))
            automation_writes.append(_step("create", AUTOMATION, automation_id, automation))
        elif have_automations[automation_id] != automation or automation_id in unloaded:
            automation_writes.append(_step("update", AUTOMATION, automation_id, automation))
    automation_removes = [
        _step("remove", AUTOMATION, i) for i in sorted(set(have_automations) - wanted_ids)
    ]
    warnings = [
        f"automation {i} is in the '{prefix}' lane but defined in HA's YAML (or under an id "
        "its config API can't address), so a deploy can't remove it"
        for i in unmanaged
        if i not in wanted_ids
    ]
    if problems:
        raise DeployError(problems)
    return {
        "prefix": prefix,
        "desired": copy.deepcopy(dict(desired)),
        "steps": [
            *helper_writes,
            *syncs,
            *automation_writes,
            *automation_removes,
            *helper_removes,
        ],
        "warnings": warnings,
    }


def _step(op: str, kind: str, item_id: str, data: Any = None) -> dict[str, Any]:
    step = {"op": op, "kind": kind, "id": item_id}
    if data is not None:
        step["data"] = copy.deepcopy(dict(data))
    return step


def _sync_step(
    prefix: str, desired: Mapping[str, Any], automation_id: str, mirror: Mapping[str, str]
) -> dict[str, Any]:
    domain, _, object_id = mirror["helper"].partition(".")
    if domain != "input_boolean" or object_id not in desired.get(domain, {}):
        raise ValueError(f"{automation_id}: {mirror['helper']} is not one of the lane's switches")
    return {
        "op": "sync",
        "id": automation_id,
        "helper": mirror["helper"],
        "entity_id": mirror["entity_id"],
    }


def _slugify(text: str) -> str:
    """Home Assistant's object-id derivation, closely enough to predict what a created
    helper will be called (ported from ha-adaptive-light)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


# -- Writing ---------------------------------------------------------------------------


async def apply(ha: HAClient, deploy_plan: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Carry out a plan against HA, step by step in plan order.

    Returns the soft-switch syncs: `synced` (helper -> the state it now holds) and
    `unsynced` (helper -> its bulb's state, when that was neither on nor off, so the
    helper was left as it was).
    """
    synced: dict[str, str] = {}
    unsynced: dict[str, str] = {}
    states: dict[str, Any] | None = None
    for step in deploy_plan["steps"]:
        op, kind, item_id = step["op"], step.get("kind"), step["id"]
        if op == "sync":
            if states is None:
                # Read once, after every helper exists and before any bridge does.
                states = {s.get("entity_id"): s.get("state") for s in await ha.get_states()}
            helper, bulb_state = step["helper"], states.get(step["entity_id"])
            if bulb_state not in ("on", "off"):
                unsynced[helper] = str(bulb_state or "missing")
                log.warning(
                    "%s is %s, so %s keeps its state", step["entity_id"], unsynced[helper], helper
                )
                continue
            if states.get(helper) != bulb_state:
                await ha.call_service("input_boolean", f"turn_{bulb_state}", {"entity_id": helper})
            synced[helper] = bulb_state
        elif kind == AUTOMATION:
            if op == "remove":
                await ha.delete_automation_config(item_id)
            else:
                await ha.save_automation_config(item_id, step["data"])
        elif op == "create":
            created = await ha.create_helper(kind, step["data"])
            landed = created.get("id") if isinstance(created, dict) else None
            if landed != item_id:
                # Usually a collision: something else (a YAML helper of the same id, say)
                # already holds it and HA silently appended a suffix.
                raise DeployError(
                    f"{kind}.{item_id}: Home Assistant created {kind}.{landed} instead; "
                    "something else already holds that id. Resolve the collision in HA "
                    "and deploy again (the stray helper is removed then)"
                )
        elif op == "update":
            await ha.update_helper(kind, item_id, step["data"])
        else:
            await ha.delete_helper(kind, item_id)
    return {"synced": synced, "unsynced": unsynced}


async def verify(
    ha: HAClient,
    deploy_plan: Mapping[str, Any],
    *,
    synced: Mapping[str, str] | None = None,
    attempts: int = VERIFY_ATTEMPTS,
    delay: float = VERIFY_DELAY,
) -> None:
    """Re-read HA and check that every write in the plan landed.

    Checks the whole lane, not just the steps: every desired helper and automation is in
    HA exactly as generated, each helper's entity has the id the automations address,
    each automation loaded (HA marks one it rejected `unavailable`), nothing removed is
    still there, and each synced soft switch holds the state it was given. Retried
    `attempts` times, `delay` seconds apart, since HA reloads written automations in the
    background; raises DeployError with what still doesn't match.

    An update is confirmed by its stored config: the entity already exists, and nothing
    HA exposes says whether the background reload has picked the new config up yet.
    """
    problems: list[str] = []
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(delay)
        problems = await _mismatches(ha, deploy_plan, synced or {})
        if not problems:
            return
    raise DeployError(problems)


async def _mismatches(
    ha: HAClient, deploy_plan: Mapping[str, Any], synced: Mapping[str, str]
) -> list[str]:
    prefix, desired = deploy_plan["prefix"], deploy_plan["desired"]
    states = await ha.get_states()
    by_entity = {s.get("entity_id"): s for s in states}
    problems: list[str] = []
    for domain in lane_domains(desired):
        want, have = desired[domain], await _helpers(ha, domain, prefix)
        for item_id, fields in want.items():
            entity_id = f"{domain}.{item_id}"
            if item_id not in have:
                problems.append(f"{entity_id} is missing")
            elif have[item_id] != fields:
                problems.append(f"{entity_id} does not hold what was written")
            elif entity_id not in by_entity:
                problems.append(f"{entity_id}: the helper exists but its entity has another id")
        problems += [f"{domain}.{i} is still there" for i in sorted(set(have) - set(want))]

    # Each automation is read back by id, not found through its entity: HA reloads a
    # written one in the background, and until it has, it has no entity (or the old one).
    wanted = {automation["id"]: automation for automation in desired.get(AUTOMATION, [])}
    entities = _automation_entities(states, prefix)
    removed = {
        step["id"]
        for step in deploy_plan["steps"]
        if step["op"] == "remove" and step["kind"] == AUTOMATION
    }
    for automation_id in sorted(set(wanted) | set(entities) | removed):
        if not _ADDRESSABLE.match(automation_id):
            continue  # unmanaged: the plan warned about it
        stored = await ha.get_automation_config(automation_id)
        if automation_id not in wanted:
            # An entity with no config that the plan didn't remove is one HA's YAML defines
            # elsewhere: not this deploy's to check.
            if stored is not None or automation_id in removed and automation_id in entities:
                problems.append(f"automation {automation_id} is still there")
        elif stored is None:
            problems.append(f"automation {automation_id} is missing")
        elif stored != wanted[automation_id]:
            problems.append(f"automation {automation_id} does not hold what was written")
        elif automation_id not in entities:
            problems.append(
                f"automation {automation_id} has not loaded (is its entity disabled in HA?)"
            )
        elif entities[automation_id] == "unavailable":
            problems.append(
                f"automation {automation_id} did not load: HA rejected it (see HA's log)"
            )

    for helper, state in sorted(synced.items()):
        actual = (by_entity.get(helper) or {}).get("state")
        if actual != state:
            problems.append(f"{helper} is {actual}, not {state} like its bulb")
    return problems


# -- One namespace, end to end ---------------------------------------------------------


async def deploy(
    ha: HAClient,
    config: Mapping[str, Any],
    prefix: str,
    *,
    attempts: int = VERIFY_ATTEMPTS,
    delay: float = VERIFY_DELAY,
) -> dict[str, Any]:
    """Generate, plan, apply, and verify one namespace; return what was done.

    See `prepare` for what is checked before anything is written. Returns `{prefix,
    created, updated, removed, sync, synced, unsynced, warnings}` (see `summarise`).
    """
    deploy_plan = await prepare(ha, config, prefix)
    report = await apply(ha, deploy_plan)
    await verify(ha, deploy_plan, synced=report["synced"], attempts=attempts, delay=delay)
    return summarise(deploy_plan, report)


async def prepare(ha: HAClient, config: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    """Validate, generate, read HA, and plan one namespace's deploy, writing nothing: the
    plan `deploy` would carry out (`summarise` it for a preview).

    The config is validated and cross-checked first, and everything the lane addresses
    (its registered references: for the switching plane every bulb and hard-switch source,
    for the adaptive lane every adaptive bulb, its soft switch and the room's sensors) must
    exist in HA (a missing bulb means the snapshot is stale: sync it first), and no toggle
    may take an HA group of its own bulbs as its source. Raises ConfigError or DeployError.
    """
    config_mod.validate(config)
    config_mod.cross_check(config)
    desired = generate_mod.generate(config, prefix)
    references = generate_mod.lane_references(config, prefix)
    if references is not None:
        await check_references(ha, references)
    mirrors = switching.bridge_mirrors(config) if prefix == switching.PREFIX else None
    expected = [automation["id"] for automation in desired.get(AUTOMATION, [])]
    live = await fetch_live(ha, prefix, expected=expected, domains=lane_domains(desired))
    return plan(prefix, desired, live, mirrors=mirrors)


async def check_references(ha: HAClient, references: Mapping[str, Mapping[str, str]]) -> None:
    """Raise DeployError unless every entity and device a lane addresses (as
    `switching.references` lists them) exists in HA, and no toggle's source is an HA
    group whose members include a bulb the lane drives."""
    states = {s.get("entity_id"): s for s in await ha.get_states()}
    problems = [
        f"{entity_id} ({who}) is not in Home Assistant" + _missing_hint(entity_id, who)
        for entity_id, who in references["entities"].items()
        if entity_id not in states
    ]
    for entity_id, who in references.get("toggles", {}).items():
        # The config can't see inside an HA group, so this is checked against HA: a
        # toggle on a group of its own bulbs is flipped again by every change it makes.
        members = ((states.get(entity_id) or {}).get("attributes") or {}).get("entity_id")
        if not isinstance(members, list):
            continue
        looped = sorted(set(members) & set(references.get("bulbs", {})))
        if looped:
            problems.append(
                f"{entity_id} ({who}) is an HA group of bulbs Light Map drives "
                f"({', '.join(looped)}): a toggle on it would set itself off again; bind the "
                "physical switch instead"
            )
    if references["devices"]:
        # Read only to check existence; the ids go nowhere else.
        devices = {d.get("id") for d in await ha.list_device_registry()}
        problems += [
            f"device {device_id} ({who}) is not in Home Assistant"
            for device_id, who in references["devices"].items()
            if device_id not in devices
        ]
    if problems:
        raise DeployError(problems)


def _missing_hint(entity_id: str, who: str) -> str:
    if who.startswith("bulb"):
        return "; sync the room snapshot first"
    if entity_id.startswith(switching.soft_entity("")):
        # A module lane commands the soft switches, which the switching lane creates.
        return "; deploy the switching plane first"
    return ""


def summarise(
    deploy_plan: Mapping[str, Any], report: Mapping[str, Mapping[str, str]] | None = None
) -> dict[str, Any]:
    """What a plan does, per kind: `created`, `updated`, `removed`, the soft switches it
    `sync`s before their bridges, and, given `apply`'s report, how those syncs went
    (`synced`, `unsynced`). Without a report it is a preview. Only the lane's own helper
    domains are listed."""
    kinds = [*lane_domains(deploy_plan["desired"]), AUTOMATION]
    done: dict[str, dict[str, list[str]]] = {
        label: {kind: [] for kind in kinds} for label in ("created", "updated", "removed")
    }
    sync = []
    for step in deploy_plan["steps"]:
        if step["op"] == "sync":
            sync.append(step["helper"])
            continue
        label = {"create": "created", "update": "updated", "remove": "removed"}[step["op"]]
        done[label][step["kind"]].append(step["id"])
    report = report or {"synced": {}, "unsynced": {}}
    return {
        "prefix": deploy_plan["prefix"],
        **done,
        "sync": sync,
        "synced": dict(report["synced"]),
        "unsynced": dict(report["unsynced"]),
        "warnings": list(deploy_plan.get("warnings", [])),
    }


# -- The crossover report --------------------------------------------------------------

# The kinds of HA item `search/related` lists that can drive a light.
_DRIVERS = ("automation", "script", "scene")


async def crossover(ha: HAClient, config: Mapping[str, Any]) -> dict[str, Any]:
    """What else in HA touches the switching plane: read-only, for a deploy preview.

    - `drivers`: automations, scripts, and scenes outside the `lm_` lane that name a
      mapped bulb (`{entity_id, kind, name, id, adaptive_light, bulbs}`), found through
      HA's search. `adaptive_light` marks the live ha-adaptive-light's own (`al_`)
      automations. Until cutover these keep driving the bulbs beside Light Map. One that
      targets a whole Area or device, rather than the bulb's entity_id, isn't found.
    - `groups`: HA groups (light groups, old-style groups) with mapped bulbs among their
      members (`{entity_id, name, bulbs}`). Commanding a group drives its members too.
    - `collisions`: names Light Map would generate that something else in HA holds: a
      soft switch's entity id taken by a helper it can't manage (the deploy refuses), or
      an automation entity id taken by another automation (ours would load as `..._2`).
    - `errors`: what couldn't be checked (an HA without `search/related`, say), so an
      empty report is never mistaken for a clean one.
    """
    bulbs = switching.references(config)["bulbs"]
    states = await ha.get_states()
    by_entity = {s.get("entity_id"): s for s in states}
    ours = {
        str(s.get("entity_id"))
        for s in states
        if str(s.get("entity_id", "")).startswith("automation.")
        and in_lane(str((s.get("attributes") or {}).get("id", "")), switching.PREFIX)
    }

    def name(entity_id: str) -> str:
        attributes = (by_entity.get(entity_id) or {}).get("attributes") or {}
        return str(attributes.get("friendly_name") or entity_id)

    drivers: dict[str, dict[str, Any]] = {}
    group_bulbs: dict[str, set[str]] = {}
    errors: list[str] = []
    for bulb in bulbs:
        try:
            related = await ha.search_related("entity", bulb)
        except HAError as exc:
            # Every search would fail the same way; say so once.
            errors.append(
                f"could not ask HA which automations, scripts and scenes use the bulbs: {exc}"
            )
            break
        for kind in _DRIVERS:
            for entity_id in related.get(kind, []):
                if entity_id in ours:
                    continue
                attributes = (by_entity.get(entity_id) or {}).get("attributes") or {}
                driver = drivers.setdefault(
                    entity_id,
                    {
                        "entity_id": entity_id,
                        "kind": kind,
                        "name": name(entity_id),
                        "id": attributes.get("id"),
                        "adaptive_light": _is_adaptive_light(entity_id, attributes.get("id")),
                        "bulbs": [],
                    },
                )
                driver["bulbs"].append(bulb)
        for entity_id in related.get("group", []):
            group_bulbs.setdefault(entity_id, set()).add(bulb)
    for state in states:
        members = (state.get("attributes") or {}).get("entity_id")
        if isinstance(members, list) and state.get("entity_id") not in bulbs:
            for bulb in set(members) & set(bulbs):
                group_bulbs.setdefault(str(state.get("entity_id")), set()).add(bulb)

    desired = generate_mod.generate(config, switching.PREFIX)
    domains = lane_domains(desired)
    helpers = {domain: {i.get("id") for i in await ha.list_helpers(domain)} for domain in domains}
    collisions = []
    for domain in domains:
        for item_id in desired[domain]:
            if f"{domain}.{item_id}" in by_entity and item_id not in helpers[domain]:
                collisions.append(
                    f"{domain}.{item_id} is already in HA but isn't one of Light Map's "
                    "UI-managed helpers (a YAML helper, or another helper's entity renamed "
                    "to it?): the deploy is refused until the id is freed"
                )
    for automation in desired.get(AUTOMATION, []):
        entity_id = f"automation.{automation['id']}"
        held_by = ((by_entity.get(entity_id) or {}).get("attributes") or {}).get("id")
        if entity_id in by_entity and held_by != automation["id"]:
            other = f"automation id {held_by}" if held_by else "an automation with no id"
            collisions.append(
                f"{entity_id} is already taken by {other} ({name(entity_id)}): Light Map's "
                f"{automation['id']} would load as {entity_id}_2"
            )
    return {
        "drivers": sorted(
            drivers.values(), key=lambda d: (not d["adaptive_light"], d["entity_id"])
        ),
        "groups": [
            {"entity_id": entity_id, "name": name(entity_id), "bulbs": sorted(found)}
            for entity_id, found in sorted(group_bulbs.items())
        ],
        "collisions": collisions,
        "errors": errors,
    }


def _is_adaptive_light(entity_id: str, automation_id: Any) -> bool:
    """The live ha-adaptive-light names its artefacts `al_...` (scene, maintenance,
    watchdog automations)."""
    return entity_id.startswith("automation.al_") or str(automation_id or "").startswith("al_")


# -- Area writes -----------------------------------------------------------------------


def plan_areas(
    config: Mapping[str, Any],
    areas: Sequence[Mapping[str, Any]],
    lights: Sequence[Mapping[str, Any]],
) -> dict[str, list[Any]]:
    """The Area writes the committed snapshot implies: `moves` (`{bulb, entity_id, from,
    to}`, `to` None for Unassigned) and `problems` (a snapshot too stale to write: a room
    whose Area HA no longer has, a bulb whose light it no longer has). Pure.

    Ignored bulbs are left out: their lights are not Light Map's to move, and one HA has
    moved, or no longer has, doesn't hold up the rest.

    `areas` is HA's Area registry; `lights` is what `inventory.discover_lights` returns.
    """
    known = {a.get("area_id") for a in areas}
    by_entity = {light["entity_id"]: light for light in lights if not light.get("is_group")}
    moves: list[dict[str, Any]] = []
    problems: list[str] = []

    def place(bulb: Mapping[str, Any], area_id: str | None) -> None:
        if inventory.is_ignored(bulb):
            return
        light = by_entity.get(bulb["entity_id"])
        if light is None:
            problems.append(
                f"bulb '{bulb['id']}': HA has no {bulb['entity_id']}; sync the snapshot first"
            )
        elif light.get("area_id") != area_id:
            moves.append(
                {
                    "bulb": bulb["id"],
                    "entity_id": bulb["entity_id"],
                    "from": light.get("area_id"),
                    "to": area_id,
                }
            )

    for room in config.get("rooms", []):
        if room["ha_area_id"] not in known:
            problems.append(
                f"room '{room['id']}': HA has no Area '{room['ha_area_id']}' any more; sync the "
                "snapshot first (Light Map can't recreate an Area under a given id)"
            )
            continue
        for bulb in room.get("bulbs", []):
            place(bulb, room["ha_area_id"])
    for bulb in config.get("unassigned", []):
        place(bulb, None)
    moves.sort(key=lambda move: move["entity_id"])
    return {"moves": moves, "problems": problems}


async def deploy_areas(
    ha: HAClient,
    config: Mapping[str, Any],
    *,
    expected: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write the committed room snapshot to HA's Area/entity registry and verify it.

    Moves each bulb whose light HA has in another Area (or none) into the Area its room
    snapshots, with `inventory.move_bulb`, which re-reads HA after each write. Never
    creates, renames, or deletes an Area, and touches no entity but the bulbs' own.
    Every move is checked first (`plan_areas`, then `inventory.check_move` for each, which
    also refuses a light whose registry entry isn't the bulb's any more): if any would be
    refused, nothing is written. Returns `{moves}`. To preview, call `plan_areas` with HA's
    Areas and lights, and pass the previewed moves as `expected`: if HA has changed since,
    so the moves differ, nothing is written (StalePreview).

    A failure once writing has begun raises DeployError with `written` set to the moves
    that landed: the next preview starts from whatever HA then holds.
    """
    deploy_plan = plan_areas(config, await ha.list_areas(), await inventory.discover_lights(ha))
    if expected is not None and deploy_plan["moves"] != list(expected):
        raise StalePreview(
            "HA's Areas have changed since the preview: preview the Area writes again"
        )
    keys = match_keys(config)
    problems = list(deploy_plan["problems"])
    for move in deploy_plan["moves"]:
        reason = await inventory.check_move(
            ha, move["entity_id"], move["to"], match_key=keys.get(move["bulb"])
        )
        if reason:
            problems.append(f"bulb '{move['bulb']}': {reason}")
    if problems:
        raise DeployError(problems)
    landed: list[dict[str, Any]] = []
    try:
        for move in deploy_plan["moves"]:
            await inventory.move_bulb(
                ha, move["entity_id"], move["to"], match_key=keys.get(move["bulb"])
            )
            landed.append(move)
        after = plan_areas(config, await ha.list_areas(), await inventory.discover_lights(ha))
    except (InventoryError, HAError) as exc:
        raise DeployError(str(exc), written=landed) from exc
    if after["moves"] or after["problems"]:
        raise DeployError(
            [f"{m['entity_id']} is not in {m['to'] or 'Unassigned'}" for m in after["moves"]]
            + after["problems"],
            written=landed,
        )
    return {"moves": deploy_plan["moves"]}


def match_keys(config: Mapping[str, Any]) -> dict[str, str]:
    """Bulb id -> its `ha_match_key`, for every bulb that has one: what an Area write
    checks HA's registry entry against, so it can't move a different light that has
    since taken the bulb's entity_id."""
    bulbs = [b for room in config.get("rooms", []) for b in room.get("bulbs", [])]
    bulbs += list(config.get("unassigned", []))
    return {b["id"]: b["ha_match_key"] for b in bulbs if b.get("ha_match_key")}
