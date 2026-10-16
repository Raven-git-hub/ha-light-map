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
   `input_boolean`. Switching groups (UI: "group soft switch") are *not* auto-generated —
   the user adds them per room, picks member bulbs and the physical switch, and can reopen
   to edit. A group mints no HA entity of its own (settled in P2): it is a named set of
   member bulbs plus its hard-switch bindings, and a hard switch bound to it acts on every
   member's soft switch.
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
  - `bulbs[]`: `{ id, name, entity_id (light.*), ha_match_key?, hard_switches[] }` — a
    soft-switch helper is implicit (generated) per bulb. `ha_match_key` is an opaque match
    key (see "Bulb identity").
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
  in Unassigned). Named `LM Soft <bulb_id>`: HA gives a helper created over the network the
  id of its slugified name, with no way to ask for another, so the name is derived from the
  bulb's id (already a slug), never from its display name.
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
  identity" below). A moved bulb follows its Area with its id and hard switches; a light with
  no (known) Area goes to `unassigned`; a bulb whose light HA no longer has is dropped; a new
  light becomes a new bulb (id from its object id, unique among bulb and switching-group ids,
  and never one a bulb in the old snapshot used). Its name and entity_id follow HA's.
- **Switching-group and learning-unit members that leave the room are dropped**, and a group
  or unit left empty is dropped with it.
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
- **The key is opaque.** It is a match key for the reconcile only. Nothing addresses a bulb by
  it, validates it against HA, or writes it into generated artefacts: everything that
  addresses a bulb uses its `entity_id`, so "entity IDs only, never device IDs" holds. The
  worst a stale key can do is make the reconcile treat a light as new, which
  `snapshot_changes` reports before anything is saved; it can never misdirect a command.
- No schema-version bump: the key is optional, so a P1 snapshot still loads, and its first
  sync records keys by matching on entity_id. Rename entities in HA only after that first
  sync; until a bulb has its key, a rename still reads as remove + add.

The writes from the Map change HA first; a sync then brings the snapshot up to date:

- **Move a bulb** sets the entity's own Area. It is refused, before writing, for a light with
  no registry entry (a YAML light without a `unique_id`, which HA can't give an Area), for an
  Area that doesn't exist, and for unassigning a bulb whose device has an Area — clearing the
  entity's Area would leave it in the device's, and unassigning the device would move its other
  entities too, which is the user's call in HA. After writing it re-reads the registry and
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
   config removes its soft switch and bridge and leaves the bulb exactly as it is.

No step calls a light service. Before writing, a switching-plane deploy validates the config
and checks that every bulb and hard-switch source exists in HA (a missing bulb means the
snapshot is stale: sync first); a device source's device must still be in the device
registry. An automation id already defined in HA's YAML outside `automations.yaml` (which
the config API can't edit) is refused rather than duplicated.

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

**Area writes** (`deploy_areas`) make HA's Areas match the committed room snapshot, through
the inventory's own verified `move_bulb`. Every move is checked first (`inventory.check_move`
answers what `move_bulb` would refuse) and if any would be refused — or the snapshot is too
stale to write: a room whose Area HA no longer has, a bulb whose light it no longer has —
nothing is written. It never creates, renames, or deletes an Area. It is a separate call
from the switching deploy (which needs no Areas), and `plan_areas` previews it: because
edits made from the Map reach HA first, a difference usually means HA moved on, and writing
the snapshot back reverts that, so it should be reviewed before it is applied.

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
