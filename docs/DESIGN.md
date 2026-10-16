# Light Map — Design

Why the system is built the way it is: the two-plane reframe, the three-layer model, the
soft→bulb bridge, learning units, and the data model. Structure, layout, the config shape
summary, storage, naming and the roadmap are in `docs/ARCHITECTURE.md`. Invariants are listed
there under "House rules"; this document gives the reasoning behind them.

---

## The reframe: two planes

The predecessor, Adaptive Light, did two jobs through one action: at a crossover it decided
both *whether* a light was on (turn it on, or "off mode" turned it off) and *how bright*. A
house already runs its lights through hard and soft switches — which is really a dedicated
**switching plane** that wants to own the first job. Splitting them:

- **Switching plane (core).** Every bulb has a soft switch (`input_boolean`); any number of
  hard switches bind to it; the soft switch is the single source of truth for the light's
  power. Hard switches, modules, and the user act on the soft switch, never the bulb
  directly. A soft→bulb bridge makes the bulb follow.
- **Adaptive plane (a module).** Owns *brightness when on* for the rooms flagged adaptive. It
  no longer powers bulbs on and off — it commands the soft switch.

The map is the foundation adaptive light sits on, and is useful on its own: a room never
flagged adaptive still gets the full switching plane.

---

## The three layers (and the trap they avoid)

Three *independent* groupings, with the **bulb** as the common actuation atom. They often
coincide, but forcing them to be the same is a mistake:

- **Room** — a Home Assistant Area; contains bulbs. Where membership is edited.
- **Switching group** — a power fan-out: one hard switch toggles several bulbs together.
  User-created on demand per room (the "group soft switch").
- **Learning unit** — a set of bulbs that share one learned brightness target per section.
  Defined in the adaptive config, independent of switching groups.

**Motivating case (living room, 7 bulbs).** Three bulbs sit in one chandelier — essentially
one light, so for *learning* they pool into a single unit. The user also wants one smart
switch that turns *every* light in the room off — so for *switching* all seven belong to one
group. The two partitions are different shapes: one switching group of seven, but a learning
partition of {chandelier ×3} + four singletons. A per-switching-group "learn together /
individually" toggle cannot express that, so learning units are a **free partition** of the
room's adaptive bulbs.

A learning unit shares a *target*, not power: its members keep their own soft switches and
can be on or off independently; maintenance only ever nudges the ones that are on.

---

## Decisions

1. **Rooms come from HA Areas, edited two-way.** The Map lists every `light.*` on the
   network, bucketed by the bulb's HA Area; bulbs with no Area sit in an "Unassigned" bucket.
   Editing a bulb's room in the Map writes the Area reassignment back to HA; renaming or
   creating a room renames/creates the HA Area. HA Areas stay the source of truth; the config
   keeps a committed snapshot of the resolved mapping so generation stays deterministic.
2. **Adaptive is a per-room flag.** A checkbox beside each room name turns adaptive on for
   that room and reveals its Adaptive Config. Rooms left unchecked are switching-only.
3. **Soft switches are generated, per bulb only.** Each bulb automatically gets an
   `input_boolean`, unless the user ignores it (P4.1, "Ignored lights"). Switching groups
   (UI: "group soft switch") are *not* auto-generated — the user adds them per room, picks
   member bulbs and the physical switch, and can reopen to edit. A group mints no HA entity
   of its own (settled in P2): it is a named set of member bulbs plus its hard-switch
   bindings, and a hard switch bound to it acts on every member's soft switch.
4. **The soft switch owns on/off; modules command the soft switch.** Adaptive stops toggling
   bulbs directly. At a crossover it sets each adaptive bulb's power intent on that bulb's
   soft switch (on for an auto/learned unit, off for an "off for this section" unit); the
   soft→bulb bridge makes the bulb follow. "Off for a section" holds the soft switch off
   rather than baking a `light.turn_off` into the scene.
5. **Learning units are a free partition.** Per adaptive room, the bulbs are partitioned into
   learning units — each one or more bulbs sharing a brightness target — independent of the
   switching groups. Default: every bulb its own unit; one click to "learn a switching group
   as a single unit" for the aligned case; custom units (including across switching groups)
   for the rest.
6. **Soft switches are `input_boolean`s — pure power toggles.** Brightness never flows
   through the soft switch; it is applied to the bulb by the bridge and the crossover, guarded
   on soft-on. (See "Soft switch entity type".)

---

## Data model

A room is the single shared container. (Config shape summary in `docs/ARCHITECTURE.md`.)
Everything downstream is a pure function of the committed document.

- `rooms[]`: `{ id, name, ha_area_id, adaptive, bulbs[], switching_groups[], adaptive_config? }`
  - `bulbs[]`: `{ id, name, entity_id (light.*), ha_match_key?, ignored, hard_switches[] }`
    — a soft-switch helper is implicit (generated) per bulb that isn't ignored.
    `ha_match_key` is an opaque match key (see "Bulb identity"); `ignored` (default false)
    sets a light that isn't a room bulb aside (see "Ignored lights").
  - `switching_groups[]`: `{ id, name, members (bulb ids), hard_switches[] }` — a named set
    of member bulbs plus its hard-switch bindings. It has no soft switch of its own: a hard
    switch bound to the group acts on each member's.
  - `adaptive_config` (required when `adaptive` is true, forbidden when false):
    `{ lux_sensors[], presence_sensors[], schedule_profile, learning_units[], scenes }`.
    `learning_units[]` = `{ id, name, members (bulb ids) }`; `scenes` is the per-section matrix
    keyed by learning-unit id × section (per-unit, per-section `mode` auto/off, plus lux
    margin, maintenance flag, transition, max step).

  `hard_switches[]` is the binding shape used inside `bulbs[]` and `switching_groups[]` — not a
  key on the room. Shape: `{ id, name, source, mode, turn? }`: `source` is a tagged union
  (`state` / `event` / `device`), `mode` ∈ `toggle` | `follow` | `press`, and `turn` ∈ `on` |
  `off` says what a `press` does (required for a press, forbidden otherwise). A room-wide
  "all off" is a `press` with `turn: off` on a switching group covering the room, not a
  room-level binding. See "Hard switches" below.
- `unassigned[]`: bulbs whose light has no HA Area (the Map's "Unassigned" bucket), same shape
  as a room's bulbs. A bulb's `id` — which names its soft switch — and its hard switches stay
  with it as it moves between rooms and Unassigned.

The existing single room from `ha-adaptive-light` is re-expressed against this shape rather
than migrated; history starts fresh (as the n8n→container cutover did).

---

## Generated artefacts

A parallel namespace to the adaptive `al_*` artefacts, deployed and reconciled the same way
but in its own lane (settled in P2):

- `input_boolean.lm_soft_<bulb_id>` — one per bulb, and only per bulb (including the bulbs
  in Unassigned, excluding the ignored ones). Named `LM Soft <bulb_id>`: HA gives a helper
  created over the network the id of its slugified name, with no way to ask for another, so
  the name is derived from the bulb's id (already a slug), never from its display name.
- **No group entity.** A switching group mints nothing in HA. A group of `input_boolean`s
  can't be created over HA's network API (it would need a `configuration.yaml` edit, which
  Light Map never makes), and without one there is no group state to keep in sync. A hard
  switch bound to a group acts on every member's soft switch. (If a single group-state
  entity is ever wanted for dashboards, it would be an HA light group of the member bulbs,
  a future enhancement, not a soft switch.)
- `lm_hard_<hard_switch_id>` (hard→soft): one automation per binding; its trigger comes
  from the binding's source and its action from its mode (see "Hard switches").
- `lm_bridge_<bulb_id>` (soft→bulb): soft on → turn the bulb on (at a module-supplied
  brightness, or its last one); soft off → turn it off.
- The adaptive almanac (`sensor.al_almanac_<room>`) is keyed by **learning-unit id**.

Each automation's `alias` is its id, so its entity is `automation.<id>`, and its
description names the bulb or group in words. The generator returns an HA package
(`input_boolean:` object id → fields; `automation:` a list, bridges before hard switches),
which `core.generate.to_yaml` serialises for review or export; deploy sends the same data
over HA's APIs.

No template engine; byte-deterministic output (bulbs, bindings, targets and user-supplied
event data are sorted, so it doesn't depend on the order the config lists them in); no
baked-in almanac state (an adaptive bulb's bridge reads the almanac at runtime, through the
fragment the module supplies).

---

## The soft→bulb bridge (where the planes meet)

The bridge is the only thing that powers a bulb on or off. On `soft → on` it turns the bulb
on; for an adaptive bulb it applies the current section's learned brightness (the bulb's
learning-unit target), for a plain bulb its last brightness. On `soft → off` it turns the
bulb off. So flipping any hard switch brings an adaptive light up already-correct for the
time of day.

**The bridge is core; the brightness is module-contributed** (settled in P2). P2 builds the
plain path only: soft on → `light.turn_on` with no data (the bulb's last brightness). A
module supplies the brightness for the bulbs it owns through a seam in `core.switching`:
it registers a provider (`register_bridge_brightness`), which for each bulb returns the
`light.turn_on` data to apply on soft-on (`brightness`, `brightness_pct`, `transition`;
values may be HA Jinja, kept inert) or None to leave the bulb on the plain path. The core
generator embeds that fragment in the bulb's bridge, inside the soft-on branch, so:

- the almanac read lives in an adaptive-contributed fragment, not in core;
- it is guarded on soft-on by construction, and can't change the target or the power;
- at most one module may claim a bulb;
- the provider must not bake almanac values in (it emits a template that reads them at
  runtime), and must render a valid value on every path, because an invalid one fails the
  whole `light.turn_on` and the bulb would not come on.

The fragment ships in the `lm` lane (it is part of the bridge), and only *reads* `al_`
entities; the switching deployer still never writes one. The composition root registers
the provider, as it registers the module's namespace (P5).

**Firing only on a real flip.** The bridge triggers on its soft switch changing `off → on`
or `on → off`, nothing else, so a soft restored at HA startup (no state → on), created,
deleted, or going `unavailable` never drives the bulb. It runs in `restart` mode and
branches on the soft's state when it runs, not on which trigger fired: a flip arriving
mid-run replaces it, and the bulb always ends on the soft's latest state (`single` would
drop the second flip and leave bulb and soft disagreeing; `parallel` could finish them out
of order).

Brightness rides alongside power, always guarded by "soft is on", so a brightness command can
never power a bulb on:

- A crossover that changes section while a light stays on pushes the new section's brightness
  to the bulb directly (guarded on soft-on), since the soft switch's state did not change and
  the bridge would not re-fire.
- Lux maintenance nudges only already-on bulbs, brightness only, never power; it iterates
  bulbs rather than one-entity groups.

Switch-driven and bridge-driven changes all carry `context.parent_id`, so reactive detection
ignores them: toggling power is a power decision, never learned as a brightness preference. A
manual *dimmer* change on the bulb still has no `parent_id` and is still learned.

---

## Learning units and the analyser

The learning unit is primarily an *analysis* construct. Observations are stored **per bulb**
(long format) and pooled per unit at analysis time to compute one target. Keeping storage at
the bulb means units can be re-drawn later without losing history — consistent with "CSV is
the archive, SQLite is a rebuildable query layer."

Consequences: the almanac is keyed by learning-unit id; the scene matrix is keyed by
learning-unit × section (auto/off per unit per section); at a crossover every member bulb of
a unit is driven to the same power intent and, if on, the same brightness. This is a moderate
but clean extension of the ported analyser (per-group averaging over on-samples already
exists; a unit is now a set of bulbs rather than a single entity), not a rewrite.

---

## Rooms, HA Areas, and the new write surface

Membership is edited against HA Areas, two-way. To keep generation a pure function of the
config and byte-deterministic, the config holds a committed **snapshot** of the resolved
bulb→room mapping (with the `ha_area_id`); the Map keeps config and HA in sync — read Areas to
populate and refresh, write back on edit, commit the snapshot generation runs on. If someone
edits an Area directly in HA, the Map shows it stale until refreshed and a deploy uses the
committed snapshot.

Writing the Area/entity registry (reassigning a bulb's Area; creating/renaming an Area) is a
**new write surface** — the predecessor only created helpers/automations and pushed almanac
states. It gets the same care as helper deploy: verified after the fact, reconcilable, never
destructive by surprise.

### How the snapshot follows HA (settled in P1)

- **A bulb's Area is its effective Area**, resolved the way HA resolves it: the entity's own
  Area, else its device's. Most lights carry no Area of their own and inherit their device's,
  so reading the entity registry alone would put nearly every bulb in Unassigned. The device
  registry is read only for this join; device IDs never reach the config.
- **Every Area is a room**, with or without lights, so a room created from the Map appears
  before anything is moved into it.
- **A room is matched to its Area by `ha_area_id`** and keeps its id and everything user-set
  (switching groups, `adaptive`, `adaptive_config`) while the Area exists; its name follows the
  Area's. A new Area becomes a new, non-adaptive room (id from the Area id). A room whose Area
  is gone from HA is dropped.
- **A bulb is matched by its `ha_match_key`**, or by `entity_id` when it has none (see "Bulb
  identity" below). A moved bulb follows its Area with its id, hard switches and `ignored`
  flag; a light with
  no (known) Area goes to `unassigned`; a bulb whose light HA no longer has is dropped; a new
  light becomes a new bulb (id from its object id, unique among bulb and switching-group ids,
  and never one a bulb in the old snapshot used). Its name and entity_id follow HA's.
- **Switching-group and learning-unit members that leave the room are dropped** (as are
  ignored ones), and a group or unit left empty is dropped with it.
- **HA light groups are not bulbs.** A `light.*` that lists member `entity_id`s (HA's group
  platform) or is a Hue room/zone is skipped: driving it as well as its members would command
  the same lights twice. The doctor lists them.
- The reconcile is a pure function of (config, Areas, lights), independent of the order HA
  lists them in, and idempotent. It is a *proposal*: `snapshot_changes` lists every room and
  bulb it adds, removes, renames or moves and every member it drops, and nothing is written
  until the result is saved (validate-then-write). That review step is what keeps a sync from
  being destructive by surprise.

### Bulb identity (settled in P1.1)

A bulb is its light's **HA entity-registry entry**, not its `entity_id`. Renaming a light's
entity_id in HA keeps its registry entry, and matching on entity_id read the rename as one
bulb removed and another added: a new id (so a new soft switch), its hard switches gone, its
switching-group and learning-unit memberships pruned.

- On discovery each light records its registry entry's `id` as `ha_match_key`. That is HA's
  own handle for the entry: kept across entity_id renames and unique across integrations. The
  integration's `unique_id` is not used: it is unique only within its integration and often
  embeds hardware identifiers that have no place in a committed config.
- The reconcile matches a snapshot bulb to a light by `ha_match_key` first, and by
  `entity_id` only when the bulb has no key: a light outside the entity registry (a YAML light
  with no `unique_id`), or a snapshot written before keys were recorded. A matched bulb keeps
  its id, hard switches and memberships and takes the light's current entity_id (and its key,
  the first time). A keyed bulb whose key HA no longer has is gone, even when another light now
  holds its old entity_id: a different registry entry is a different bulb.
- A new bulb never takes an id a bulb in the old snapshot used. Memberships name bulbs by id,
  so a new light given a just-dropped bulb's id would inherit its group and learning-unit
  memberships (though not its hard switches) by coincidence.
- `snapshot_changes` follows bulbs by id, so a rename reads as `bulb entity_id changed`, and
  the keys a sync records for the first time are counted in one line.
- **The key is opaque.** It is a match key only. The reconcile matches on it, and (since P3)
  an Area write compares it with the registry entry now at the bulb's `entity_id` before
  moving that light: a snapshot not synced since the bulb's light was renamed in HA, and
  another light took its old entity_id, would otherwise move that other light and record
  the bulb as moved. Nothing addresses a bulb by it, requires it to exist in HA, or writes
  it into generated artefacts: everything that addresses a bulb uses its `entity_id`, so
  "entity IDs only, never device IDs" holds. The worst a stale key can do is make the
  reconcile treat a light as new, which `snapshot_changes` reports before anything is
  saved, or refuse an Area write until a sync; it can never misdirect a command.
- No schema-version bump: the key is optional, so a P1 snapshot still loads, and its first
  sync records keys by matching on entity_id. Rename entities in HA only after that first
  sync; until a bulb has its key, a rename still reads as remove + add.

### Ignored lights (settled in P4.1)

Not every `light.*` is a room bulb. Indicator LEDs (a Zigbee coordinator's, a network
cabinet's, the status LED of a switch or an access point) and gadget LEDs (a mug warmer's)
are lights to HA, so discovery finds them, and each would get a soft switch and a bridge. A
bulb marked **ignored** (`ignored: true`) is set aside:

- **It stays in the snapshot**, in its room or in Unassigned, and the reconcile follows it
  like any bulb: matched by `ha_match_key` (else by entity_id), so the flag survives a sync,
  a move between Areas and an entity_id rename, and dropped when HA no longer has its light.
  That is what lets it be un-ignored, and what keeps its id reserved.
- **Nothing else sees it.** It gets no soft switch and no bridge, so the next deploy removes
  the `lm_soft_` and `lm_bridge_` of one that was deployed, as it removes any artefact the
  config no longer calls for, and leaves the light exactly as it is. A deploy doesn't need
  its light to exist in HA; the Area writes never move it, and a Map move is refused; the
  observer doesn't watch it; it is no switching group's or learning unit's member and
  carries no hard switches (the cross-check refuses a config that says otherwise); the
  hard-switch source picker and the group member picker don't offer it. Generation and
  the observer read the bulbs through `core.inventory.active_bulbs`, and so does a module
  (adaptive, P5), so it never meets one; the rest (the Area writes, the Map's view, the
  cross-check) look at the flag themselves.
- **A toggle still may not take it as its source.** Nothing drives an ignored light, but
  a toggle on one would loop the moment it was un-ignored (or a sync gave its light to a
  new bulb), and a status LED worth ignoring may mirror a load Light Map does drive. So
  the feedback-loop check counts every bulb in the snapshot, ignored ones included.
- **Ignoring drops what hangs off the bulb**: its hard switches, and its memberships (a
  switching group or learning unit left with no members is dropped with it, a group's hard
  switches too), since none of them means anything without a soft switch. When it would
  drop anything the API asks first (409 `confirm_required`, listing it), as unflagging a
  room's adaptive config does. Un-ignoring brings the bulb back bare: a soft switch and a
  bridge on the next deploy, no bindings, nothing made live.
- **A newly discovered light is never ignored**, and nothing classifies lights for the user:
  a guess that a light is an indicator LED would hide a real bulb, so ignoring is always the
  user's explicit choice.
- Version 3 of the config added the flag. A version-2 config migrates with every bulb not
  ignored. The bump is what makes an older Light Map refuse a version-3 config outright
  rather than load the house without knowing which lights to leave alone.

The writes from the Map change HA first, and then exactly that change is committed to the
snapshot (settled in P3; see "The Map and its API"): never a full sync, which would also
commit, unreviewed, whatever else HA has changed meanwhile.

- **Move a bulb** sets the entity's own Area. It is refused, before writing, for a light with
  no registry entry (a YAML light without a `unique_id`, which HA can't give an Area), for an
  Area that doesn't exist, for a light whose registry entry isn't the bulb's (its
  `ha_match_key`; the snapshot is stale: sync first), and for unassigning a bulb whose device
  has an Area — clearing the entity's Area would leave it in the device's, and unassigning
  the device would move its other entities too, which is the user's call in HA. After writing it re-reads the registry and
  fails loudly if the bulb isn't where it was asked to go.
- **Create / rename a room** creates / renames the HA Area (its id, and so the room's, never
  changes). A name HA would reject as a duplicate is refused up front; the result is re-read
  and checked.
- All three need an administrator's token; the doctor warns when the token isn't one.

---

## Soft switch entity type (settled)

The soft switch is an **`input_boolean`** — a pure power toggle. Brightness does not enter the
switching plane; it is applied to the bulb by the bridge and the crossover, always guarded by
"soft is on" so a brightness command can never power a bulb on. This matches the generated-
helper pattern and leaves the per-bulb observation path untouched.

The alternative — a template `light` that proxies the bulb for on/off *and* brightness,
routing brightness through the soft switch too — was considered and rejected: it would move
the observation target from the bulb onto a proxy, disturbing the proven learning path, for a
purity that buys nothing here.

---

## Hard switches (settled in P2)

Each binding becomes one `lm_hard_<id>` automation acting on its **targets**: a bulb
binding's target is that bulb's soft switch; a group binding's targets are every member's.
The **source** says what fires it, and the **mode** what it does:

| `source.type` | what fires it |
| --- | --- |
| `state` (`entity_id`) | a stateful switch, binary_sensor, light, or `event.*` entity. `follow` acts on its `off→on` / `on→off` changes; `toggle` and `press` on every real change of its state (a latching switch's flip, an event entity's new timestamp), never on one to or from `unavailable` / `unknown` or on the entity appearing or disappearing, so a source that comes back from a restart or reload straight to its state is never a press (one that settles through a real change as it loads, like an HA group whose members come up one by one, does act: bind the physical switch's own entity) |
| `event` (`event_type`, `event_data?`) | a stateless button firing an HA event; `event_data` matches a subset of the event's data |
| `device` (`device_id`, `trigger`) | a device automation trigger (`trigger` holds `domain`, `type`, `subtype`, ...), only for a button that exposes no entity or event. The device id only *reads* a press: it appears in the trigger, never in a target — the one carved-out exception to "entity IDs only" |

- `toggle` — state-aware across the targets: if any target is on, turn them all off, else
  turn them all on. (Flipping each member instead would swap which lights of a half-lit
  group are on.) This is an inert HA Jinja condition, `{{ is_state(a, 'on') or ... }}`.
- `press` — set every target to `turn` (`on` or `off`) each time it fires. A one-way "all
  off" is a press with `turn: off`; pressing it twice is harmless.
- `follow` — the targets track the source's on/off. Needs a `state` source.

Hard-switch automations run `queued`: each press applies in turn and sees the last one's
result, so two quick presses of a toggle are two toggles. A soft switch may carry several
hard switches of mixed modes.

Some bindings would feed back on themselves, and are refused: a source in Light Map's own
`lm_` namespace (bind the physical switch, or use a switching group); a `toggle` whose
source is a bulb Light Map drives (its bridge's change would toggle it again, forever —
`follow` and `press` set a state rather than flip one, so they settle); and an event source
on an HA core event (`state_changed`, `call_service`, `automation_triggered`,
`automation_reloaded`, the registry updates, the start/stop events, ...), which fire for
the plane's own work, a deploy, or a restart. The config cross-check catches these; deploy
also refuses a `toggle` whose source is an HA group of bulbs Light Map drives, which only
HA's state can show. The general rule, which no check can fully enforce (a relay exposed
through `switch_as_x`, a device trigger on a bulb's own device): a toggle's source must not
be something the bulbs it switches can change.

A `toggle` on a state source flips on every change, which is right for a latching wall
switch, a relay in detached mode, or an `event.*` entity; a momentary contact (on while held,
off on release) should be bound through its event or device trigger instead, or it toggles
twice per press.

Version 1 configs (P1) took the source as a bare entity_id and gave a press no direction;
loading one migrates it to version 2: a string source becomes a `state` source, and a press
becomes `turn: off` (the only press version 1 described).

## Deploy (settled in P2)

Each namespace is deployed in its own lane: `fetch_live` reads what HA holds under the
prefix (UI-managed helpers over the WebSocket; automations found as `automation.*`
entities by their `id` attribute, read through HA's config API), `plan` diffs it against
the generated package, `apply` carries out the steps, and `verify` re-reads HA until the
whole lane matches. A plan never creates, updates, or removes anything outside its lane,
and it is a pure function, so it doubles as a preview. Ported from `ha-adaptive-light`'s
deploy: helpers named so they slugify to the id the automations address (and refused
before writing if they wouldn't), the created id checked (HA silently suffixes a taken id),
automations written by id, orphans removed. New in Light Map: lanes, verification after
every deploy, and the order below.

The step order is what keeps a deploy from driving anything:

1. Helpers are created and updated first, so no automation references a missing one.
2. **Before a bridge first goes live, its soft switch is set to its bulb's current on/off.**
   The bridge doesn't exist yet, so nothing follows the change; once it does, soft and bulb
   already agree, and it fires only when something next flips the soft. An unavailable
   bulb leaves its soft as it is (a new one starts off), reported as `unsynced`. This
   applies whenever a bridge is created, not only on the first deploy, so a deploy
   interrupted between creating a soft switch and its bridge still syncs it next time.
3. Automations are written in the generator's order — bridges before hard switches — so no
   hard switch goes live before the bridges of the bulbs it switches.
4. Automations are removed before the helpers they reference. Removing a bulb from the
   config, or ignoring it, removes its soft switch and bridge and leaves the bulb exactly
   as it is.

No step calls a light service. Before writing, a switching-plane deploy validates the config
and checks that every bulb it generates for (every one not ignored) and every hard-switch
source exists in HA (a missing bulb means the snapshot is stale: sync first); a device
source's device must still be in the device registry. An automation id already defined in
HA's YAML outside `automations.yaml` (which the config API can't edit) is refused rather
than duplicated, and so (since P3) is a soft switch whose entity id HA already has as a
helper the plane can't manage (a YAML `input_boolean`): created anyway, it would land under
`<id>_2`, and the post-create check would stop the deploy only after leaving that stray
behind.

`verify` checks the whole lane, not just the steps: every helper and automation holds what
was generated, each helper's entity has the id the automations address (one renamed in HA
is caught), each automation loaded (HA marks one it rejected `unavailable`), nothing
removed is still there, and each synced soft switch holds its bulb's state. HA reloads a
written automation in the background, so verification retries for a few seconds before
reporting what still doesn't match; an update is confirmed by its stored config, since HA
exposes nothing that says the reload has picked it up. A failed or interrupted deploy is
reconciled by the next one, which plans from whatever HA then holds.

HA lists automations only as entities, so deploy reads each automation it wants by id from
the config API rather than trusting the entity list. An automation stored but not loaded
(its entity disabled, `unavailable`, or still reloading after an interrupted deploy) is
written again, which reloads it, but never "created" again: re-creating a bridge would
re-sync its soft switch to the bulb while the bridge may already be live, driving it.

The `lm_` prefix is reserved: any helper or automation in HA whose id starts with `lm_`
belongs to the plane, and one the config doesn't call for is removed (the doctor's preview
lists every removal). An `lm_` automation that HA's YAML defines outside `automations.yaml`
can't be managed: deploy refuses to write over one, and warns about the others.

**The crossover report** (`deploy.crossover`, P3) is read-only and shown beside every
deploy preview: the automations, scripts and scenes outside the `lm_` lane that name a
mapped bulb (found through HA's `search/related`, by entity id, so one that targets a whole
Area or device isn't found; the live ha-adaptive-light's `al_` automations are flagged),
the HA groups with mapped bulbs among their members (from HA's states), and the names the
plane would generate that something else holds (a YAML helper, an automation entity id
taken by another automation, so ours would load as `..._2`). What couldn't be checked is
listed too, so an empty report is never mistaken for a clean one.

**Area writes** (`deploy_areas`) make HA's Areas match the committed room snapshot, through
the inventory's own verified `move_bulb`. Every move is checked first (`inventory.check_move`
answers what `move_bulb` would refuse) and if any would be refused — or the snapshot is too
stale to write: a room whose Area HA no longer has, a bulb whose light it no longer has —
nothing is written; given the moves its preview showed (`expected`), it also writes nothing
if HA has moved on since. A failure once writing has begun says which moves landed (the
error's `written`), so a half-done write is never reported as a refusal; the next preview
plans from whatever HA then holds. It never creates, renames, or deletes an Area. It is a separate call
from the switching deploy (which needs no Areas), and `plan_areas` previews it: because
edits made from the Map reach HA first, a difference usually means HA moved on, and writing
the snapshot back reverts that, so it should be reviewed before it is applied.

---

## The Map and its API (settled in P3)

The Map tab is the first thing a person uses, and the first that deploys to a real HA, so
its shape follows from two rules: nothing reaches HA, or the committed config, that the
user wasn't shown first, and nothing makes a bulb live beside the old system silently.

**Layout.** Rooms (HA Areas, by name) down the page, then Unassigned, then a collapsed
"Ignored (N)" section listing every ignored light (P4.1) with its room and an Un-ignore
action. Per room: the name (click to rename the Area) and the ADAPTIVE checkbox; its
bulbs that aren't ignored, each by name and entity_id with Move and Ignore actions and its
generated soft switch, which opens the hard-switch popup; a groups
column (each group opens its popup; ADD makes one: name, members from the room's bulbs,
hard switches); and the Adaptive Config button, enabled only while the room is adaptive,
which opens a placeholder until the adaptive editor arrives (P5). Ticking ADAPTIVE writes a
minimal `adaptive_config` (`{}`, filled by the defaults on save); unticking drops it (the
schema forbids it on a room that isn't adaptive), so when it holds anything a user set the
API refuses until the request confirms the loss (409 `confirm_required`, listing it).

**Live.** A bulb is live when a hard switch drives it, bound to it or to a group it is in:
once deployed, pressing that switch drives the bulb through its bridge. During the
parallel build that is the coexistence boundary, so the Map marks every live bulb and
group, and the hard-switch and group popups warn before saving any edit that makes a hard
switch drive something new (a new binding, a changed source, mode or turn, or a member
added to a bound group: `inventory.going_live`). The warning needs its box ticked; the API
enforces the same rule (409 `coexistence` unless `acknowledge_live`), so the UI can't be
the only guard. Renaming or removing a binding makes nothing newly live and doesn't ask.

**The hard-switch popup** edits one soft switch's (or one group's) bindings as a staged
list, saved together. A binding's source is chosen as an entity (`state`, picked from HA's
switches, binary sensors, lights, input booleans and event entities, or typed) or an HA
event (`event`: an event type, with optional event data to match) as the primary paths;
the `device` source sits under "Advanced" as manual entry (a device id and the trigger's
fields, copied from an HA automation), so the device registry is never listed to the UI
and device ids stay confined to the trigger that reads a press. The modes are explained
in one line each. The form refuses what the cross-check would (a toggle on a mapped bulb,
an `lm_` source, follow without a state source), for the user's sake; the server checks
again on save.

**Previews and fingerprints.** Every action that writes HA, or commits HA's state to the
config, is a preview followed by an apply: sync from HA (`snapshot_changes`), a bulb move
(the snapshot changes, including the memberships it drops, and whether HA would refuse it),
the switching deploy (the plan, the hard switches it puts live, the crossover report, the
coexistence note), and the Area writes (the moves, and why any would be refused). A preview
writes nothing and returns a `fingerprint`, a digest of what it showed; the apply carries
it, recomputes from fresh state, and is refused if the digest differs. Generation is
deterministic, so an unchanged plan always digests the same, and a deploy previewed before
someone changed the config, or before HA moved on, can't be applied. The switching deploy
and the Area writes stay separate: the former never writes an Area, the latter never a
helper or automation.

**Area edits commit only themselves.** Moving a bulb, or creating or renaming a room, writes
HA first (verified, through `core.inventory`), then commits that one change to the
snapshot. The commit reuses the reconcile itself: `config.snapshot_as_ha` gives the Areas
and lights that reproduce the committed snapshot exactly (reconciling against them changes
nothing), the edit changes one of them (a light's Area, a new or renamed Area), and the
reconcile records it as a sync would, pruning a moved bulb's memberships in the room it
leaves and deriving a new room's id, without picking up anything else HA has changed. A
later sync then shows only what really differs. The edit's config is validated before the
HA write; if saving fails after it, the error says the HA change stands and a sync will
pick it up.

**Edits carry the version they were made against.** The Map shows a `version` for each
bulb's hard switches (and whether it is ignored), each switching group, and each room's
adaptive flag and config, and an edit sends it back as `base`; one made against an older
version is refused (409 `edit_stale`) and the popup closes onto the Map as it now is. Edits
replace whole lists (a bulb's bindings, a group's members), so without this a popup left
open in a second tab would silently undo the first tab's edit when saved, and an
acknowledgement (going live) or confirmation (dropping an adaptive config) given for the
view on screen would cover bindings or settings the user never saw.

**Ignoring a bulb** (P4.1) is a config edit like these: `PUT /api/bulbs/{bulb}/ignored`,
against the bulb's `ignore_version`, which also covers what ignoring it would drop (its
memberships, and any group or unit that would go with it), so a confirmation can't drop
something added since the Map showed the list. It is kept apart from the bulb's `version`,
so renaming a group in another tab doesn't make an open hard-switch popup stale. A request
for what is already so is answered `no_change` before the version is checked, so a second
click isn't reported as a stale Map. Ignoring takes effect in HA only on the next deploy,
whose preview lists the soft switch and bridge it removes. A bulb whose ignoring drops
nothing (no hard switches, no memberships) is ignored at once, so setting a dozen indicator
LEDs aside is a dozen clicks; otherwise the Map lists the drops and asks.

**The committed config** lives at `LM_CONFIG_PATH` and is read fresh for every request (it
is small, and a hand edit is picked up at once), written only through `config.save`, one
edit at a time under a lock held across the HA writes. Before the first save there is no
file: the Map starts blank and the first sync, reviewed, creates it; creating a room
before then is refused (409 `sync_first`), since it would save a config holding only that
room. A config on disk that doesn't load is never overwritten from the Map; every write is
refused until it is fixed by hand.

**A dialog stays open while its request runs.** Dismissed mid-save, it would drop the
save's answer, and the Map would go on showing what was there before a write that did
land; so Esc, × and Cancel wait for the request to settle.

---

## Observation (settled in P4)

The observer (`core.observe`) is a platform service: it watches the mapped bulbs, records
what it sees, and hands each observation to the modules that subscribe. It collects nothing
module-specific itself. Where a module's knowledge belongs in an observation (the section a
room is in, a guard it raises while it changes lights), the module supplies it through a
hook on the observer (`register_section`, `register_guard`).

**One subscription, kept alive.** The observer subscribes to HA's `state_changed` over the
WebSocket (`HAClient.subscribe_events`) and keeps the events for what it watches: the bulbs
of every room that has any, those rooms' sensors, and the guards. The read loop starts
before the subscribe command (house rule). A subscription lasts as long as the client: when
the connection drops, the reconnect ladder (`homeassistant.reconnect_backoff_seconds`)
reopens it and subscribes again, and a connection that fails to re-subscribe is closed and
retried rather than left looking connected while deaf, which is how the predecessor's
stream died silently. A rejected token stops the ladder; it never fixes itself. Events
reach handlers from a queue, in order, outside the read loop, so a handler that sends a
command can't deadlock waiting on the loop it is blocking. After every (re)connect the
observer re-reads the states it watches, since events were missed while it was away.

It runs when HA is configured and the config has a room with bulbs. The config is re-read
at every tick and after every save from the Map, so the first sync starts it without a
restart; a config that stops loading leaves it on the last one that did.

**Heartbeats.** Every `system.heartbeat_interval_minutes`, aligned to the clock (a 10-minute
cadence ticks at :00, :10, ...), each room with bulbs records one heartbeat and one
reading per bulb (long format) from a fresh read of HA's states:

- A reading is the bulb's on/off and brightness (0–255, when on and reported). `is_on` is
  null when HA has no usable state for it (unavailable, unknown, missing), so an unreachable
  bulb is never counted as off.
- `lux` comes from the room's lux sensors: the mean of those reading above 0. A reading of 0
  is the sensor's floor, not a measurement (sensors sit at 0 all night with the lights
  visibly on), so it never drags a real reading down. When every available sensor reads 0,
  `lux` is 0, the floor, which the analyser keeps out of lux targets (P5). Null when no
  sensor is configured or available; `lux_sensors` counts the sensors the value came from.
- `occupancy` is 1 if any presence sensor is on, 0 if none is, and null when none is
  configured or available. Presence sensors are read as binary sensors (`on` is
  occupied); a `person` or `device_tracker` reports `home`, which reads as not occupied. Recorded, not interpreted: what a room with no sensor means for
  learning (the predecessor treated it as always occupied) is the analyser's call.
- `section` is whatever the module with sections reports for the room (adaptive's
  scheduler, P5), null until then. It is recorded at observation time, never inferred later
  from the clock, which is what misfiled the predecessor's early history by season.
- **Never sampled mid-transition.** A heartbeat waits while any guard on its room is raised,
  recording the wait (`deferred_ms`), and is skipped with a warning if the guard is still up
  after 90 seconds. A guard is an entity a module raises while its own automations are
  changing the room's lights (`al_active_<room>`, P5). There is none before P5.

Rooms without bulbs aren't observed, and an ignored bulb (P4.1) is never read: a room
whose bulbs are all ignored isn't observed either. A room's lux and presence sensors are
read from its `adaptive_config`, where the config shape keeps them (an open decision in
`docs/ARCHITECTURE.md`).

**Reactive detection.** A user intervention is a brightness change: a bulb that was on and
is still on, at a different brightness, carrying no `context.parent_id`, not made under an
automation run's own context, while no guard on its room is raised
(`is_user_intervention`). An automation fired by a state change or an event gets the
trigger's context as its parent, so everything the plane does carries a `parent_id` (a
hard switch, the bridge and the brightness it applies on soft-on), as does an automation
fired with `automation.trigger`. An automation fired by time, a time pattern or the sun
doesn't: HA starts it with a fresh context, so its `light.turn_on` looks exactly like a
hand on a dimmer. HA does fire `automation_triggered` under that run's context, so the
observer subscribes to it too and remembers each run's context id (for six hours, the
newest 20,000), and a change made under one is never counted. A dimmer at the wall or a
slider in HA's UI has neither and counts. A power change is never a brightness preference:
through the plane it carries a `parent_id`, and out of band it is a power decision (see
"Out-of-band power changes").

The guard is there for a gap no context closes: HA keeps a service call's context on an
entity's state writes for only five seconds. A transition longer than that reports its
later steps under a fresh context with neither mark, so a module driving a long transition
must raise its guard for the whole of it, or its own ramp is learned as the user's. Until
cutover the live `ha-adaptive-light` drives the same bulbs, raising only its own guards,
which Light Map doesn't watch: the late steps of its maintenance transitions can be
recorded as manual changes. Two other cases look like a dimmer and can't be told apart
from one: a bulb power-cycled at a plain wall switch that comes back at its power-on
brightness without HA ever marking it unavailable, and another system driving lights
through HA's API under its own HA user (recorded, so the analyser can set it aside).

Interventions are consolidated per room. The first opens a window of
`system.reactive_window_seconds`; when it closes, one reactive event records the room:
every bulb before (its state when the window opened, or just before the user's first
change to it) and after, the lux before and after, and occupancy. For a bulb the user
changed, after is where their last change left it, with the HA user who made it (none for
a device); it is not re-read at the close, because by then the plane or an automation may
have moved the bulb, and that is not the user's choice. The other bulbs are recorded as the
close finds them, for context. A bulb is `changed` when the user touched it and it ended
more than `system.reactive_min_delta` from where it started; a window where none did is
dropped as rounding noise or transition echo. Someone adjusting three bulbs over a few
seconds is one intervention, not a dozen. Occupancy is recorded but never filters a
reactive event: a hand on a dimmer is proof of presence. Stopping the observer stops taking
events, then closes any open window.

**Fan-out.** Each heartbeat and reactive event is written to the store first, then handed to
every handler registered with `subscribe` (`{"kind": "heartbeat" | "reactive", ...}`). A
failing handler is logged and stops neither the others nor the observer.

**The event log** is a feature, not debug output: it records what the platform did and saw
for the Log tab. Connects and drops, reactive events, skipped heartbeats, a summary of each
heartbeat tick (at `debug`, hidden by default), store rebuilds, and the actions taken from
the Map (sync, deploy, Area writes, moves, room and config edits). Entries are archived to
CSV like any observation; `system.event_log_retention_days` prunes the query layer, and the
archive keeps everything.

**Storage.** `core.store` writes each row to its CSV stream first, flushed to disk, then to
SQLite: a database failure never costs an observation, and the query layer can always be
rebuilt from the archive (`rebuild_from_csv`). Each stream mirrors its table column for
column, minus the surrogate ids: a reading names its heartbeat by (`room_id`, `ts`), so the
archive reads back on its own. A heartbeat's readings are written before the heartbeat, so
a write cut short leaves readings with no heartbeat, which a rebuild skips, never a
heartbeat silently missing its readings.

The query layer records each archive file's size and mtime as of the last write it holds
all of, and moves that record forward only from where it was. Once a write reaches the
archive but not the database, the record stays behind for good (the store reports itself
`stale`), so the next successful write can't cover the gap, and the next start rebuilds.
At startup any mismatch (a deleted database, one left behind, an archive file edited)
triggers a rebuild; a database from another schema version (a checksum of the DDL) is
recreated; one that won't open or read cleanly is moved aside, never deleted, and rebuilt.

The archive is guarded against torn writes. A write that fails is truncated back to where
the file was; a file that doesn't end in a newline (cut short some other way) is never
appended to, writing moving on to a numbered successor, as it does for a file whose header
doesn't match the current columns; and re-ingest drops a last line without its newline,
since the CSV reader would take `12` torn from `128` as a whole value. Re-ingest reads each
file by its own header and skips what it can't read (a byte that isn't UTF-8, a value out
of range, a line a spreadsheet mangled), counting and logging it, so a damaged archive
never stops a start: with no store at all, the Map still serves.

---

## Terminology

- **Bulb** — one `light.*` entity; the actuation atom.
- **Switching group** (UI: "group soft switch") — a set of bulbs toggled together by a hard
  switch. Keeps the word "group". Despite the UI name it has no soft-switch entity: its hard
  switches act on the members' soft switches.
- **Learning unit** — a set of bulbs the adaptive learner pools into one brightness target.
  The almanac key.

---

## Side effect: Presence Rules nearly free

The two-plane split makes a future Presence Rules module nearly free: presence simply commands
the soft switch, like any other input to the switching plane.

---

## Open items

- **Adaptive-config revisions** — the adaptive editor will be close to the predecessor's
  Config view but with adjustments not yet specified; they shape P5.
- **Old-data re-ingest at cutover** — start the almanac fresh vs re-ingest the predecessor's
  per-group CSV where a group maps cleanly to one bulb.
- **Per-bulb detail** (dimmable, colour-temp) — deferred; a non-dimmable bulb is
  switching-only even in an adaptive room.
- **Out-of-band power changes** — a bulb switched outside the plane (HA's UI, a vendor app,
  voice, or the live `ha-adaptive-light` during the parallel build) leaves its soft switch
  stale: the bridge only follows the soft, never the reverse. The next toggle may then
  first "turn off" a bulb that is already off. Whether to reflect a bulb's power back into
  its soft switch (and how, without that change re-triggering the bridge) is open; settle
  it before cutover.
