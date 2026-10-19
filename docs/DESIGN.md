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
- **Adaptive plane (a module).** Owns *brightness when on* for the adaptive bulbs of the
  rooms flagged adaptive. It no longer powers bulbs on and off — it commands the soft
  switch.

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
5. **Learning units are a free partition.** Per adaptive room, the adaptive bulbs are
   partitioned into learning units — each one or more bulbs sharing a brightness target —
   independent of the switching groups. Default: a bulb made adaptive is its own unit; one
   click to "learn a switching group as a single unit" for the aligned case; custom units
   (including across switching groups) for the rest. Within an adaptive room a bulb may
   also stay switched-only (settled in P5.0; see "The adaptive editor").
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
    `{ lux_sensors[], presence_sensors[], schedule_profile, adaptive_bulbs[],
    learning_units[], scenes }`. `adaptive_bulbs` lists the room's adaptive bulbs (the rest
    are switched-only); `learning_units[]` = `{ id, name, members (bulb ids) }` partitions
    exactly the adaptive bulbs; `scenes` is the matrix over the six sections: per section
    the room-wide tuning knobs (lux target seed, lux margin, max step, maintenance flag,
    transition) and a `mode` (auto/off) per learning unit. See "The adaptive editor".

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
  brightness when the module has one for it, else at its last one); soft off → turn it off.
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
learning-unit target), for a plain bulb (including a switched-only bulb in an adaptive room)
its last brightness. On `soft → off` it turns the
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
  runtime), and must render a valid value on every path it applies its data, because an
  invalid one fails the whole `light.turn_on` and the bulb would not come on.

**The gate** (settled in P5.2a). A module doesn't always have a brightness for a bulb it
claims: Adaptive Light has none for a unit nothing has been learned for, or one held off
for the section, and a bridge that had to render *some* brightness brought such a bulb on at
255 (an off bulb reports no brightness to keep), so a hard switch pressed at 3 a.m. in a
sleep section held off came on at full brightness. So a provider may return its data
gated (`core.switching.GatedBrightness(when, data)`): `when` is a condition template, and
the bridge's soft-on branch becomes "when → `light.turn_on` with the data; else → the plain
`light.turn_on`", which HA answers by restoring the bulb's own last brightness, as for a bulb
no module claims. The guarantees hold: the gate only chooses which of two `light.turn_on`s
runs, both on the bridge's own target and both inside the soft-on branch, so a module still
can't power a bulb or retarget the bridge, and its data may still set only `brightness`,
`brightness_pct` and `transition`. The gate and the data are rendered together, in one
`variables` step, before the choice: rendered apart, a republish landing between the two
could leave the data with nothing valid to render for a gate that had just said yes, and
the bulb wouldn't come on. A provider that returns a plain mapping (ungated) applies it on
every soft-on, as before.

The fragment ships in the `lm` lane (it is part of the bridge), and only *reads* `al_`
entities; the switching deployer still never writes one. The composition root registers
the provider, as it registers the module's namespace (P5.1, both behind the cutover switch:
see "The adaptive generator").

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
learning-unit × section (auto/off per unit per section; the tuning knobs are per section,
room-wide); at a crossover every member bulb of
a unit is driven to the same power intent and, if on, the same brightness. This is a moderate
but clean extension of the ported analyser (per-group averaging over on-samples already
exists; a unit is now a set of bulbs rather than a single entity), not a rewrite. How the
pooling is done (settled in P5.2a) is in "The adaptive engine", "The analyser".

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
which opens the room's adaptive editor (P5.0; see "The adaptive editor"), with a line
saying how many of its bulbs are adaptive, in how many learning units. Ticking ADAPTIVE
writes a minimal `adaptive_config` (`{}`, filled by the defaults on save: every bulb
starts switched-only, so ticking the box alone changes nothing in how any bulb behaves);
unticking drops it (the schema forbids it on a room that isn't adaptive), so when it holds
anything a user set the API refuses until the request confirms the loss (409
`confirm_required`, listing it).

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

## The adaptive editor (settled in P5.0)

P5 lands in three steps. **P5.0** is the adaptive per-room config and its editor: it
generates and deploys nothing, and nothing new reaches HA. **P5.1** adds the adaptive
generator (the `al_` lane, and the bridge-brightness fragment that brings an adaptive bulb
on at its unit's target), behind the cutover switch (see "The adaptive generator").
**P5.2a** ports the scheduler (with the schedule profile's
internal shape and `tools/sweep.py`), the analyser, the almanac and the runtime loop (see
"The adaptive engine"); **P5.2b** adds the Now / Analysis / Almanac views and the
schedule-profile editor (see "The adaptive views").

**The block.** A room's `adaptive_config`, present exactly when the room is adaptive:

- `lux_sensors[]`, `presence_sensors[]` — the room's sensors (the observer already reads
  them here). A lux sensor is a `sensor.*`. A presence sensor must read on/off: a
  `binary_sensor.*` or an `input_boolean.*` (a `person` or `device_tracker` reports
  `home`, which the observer would read as unoccupied, always). Neither may be one of
  Light Map's own entities (`lm_`, `al_`). Whether they exist in HA is a deploy-time check.
- `schedule_profile` — the id of an entry in the global `schedule_profiles` (default
  `default`). The editor selects one by id and name. A profile's internals (its section
  boundaries, triggers and collision policy) arrived with the scheduler port (P5.2a; see
  "Schedule profiles"); their editor with P5.2b (see "The schedule editor"), which the
  picker links to.
- `adaptive_bulbs[]` — the room's adaptive bulbs, by id. **Adaptive-ness is not a flag on
  the bulb.** It lives in the module's own block, so the core bulb stays module-free and
  `app/core` never imports `app/modules`. Within an adaptive room each active (not
  ignored) bulb is in one of two tiers:
  - *adaptive* (listed): it learns, and from P5.1 comes on at its unit's learned
    brightness and follows the scenes;
  - *switched-only* (not listed): it keeps the plain switching plane, its bridge turning it
    on at its last brightness exactly as in a room that isn't adaptive. It needs no config
    and no change to the bridge.

  An ignored bulb is in neither tier: the editor doesn't offer it, and the config can't
  list it.
- `learning_units[]` — `{ id, name, members }`, partitioning **exactly** the adaptive
  bulbs: every member is an adaptive bulb of the room, and every adaptive bulb is in
  exactly one unit. A bulb made adaptive in the editor starts in a unit of its own (its
  bulb id as the unit id where that is free, its name as the unit's); "learn this switching
  group as one unit" pools a group's members in one click (making them adaptive); a unit
  may cross switching groups. Unit ids are unique within the room and key the almanac, so a
  unit keeps its id when it is renamed.
- `scenes` — the scene matrix over the six fixed sections (`sunrise`, `day`, `afternoon`,
  `sunset`, `night`, `sleep`). It is the predecessor's `scenes` block with its per-group map
  made per learning unit:

  ```
  scenes.<section> = {
    lux_target_seed       null (default) = observe; else a seed for the section's lux target
    lux_margin            deadband either side of the target (default 5)
    max_step_pct          largest single maintenance nudge, a fraction in (0, 1] (default 0.04)
    maintenance_enabled   default false for night and sleep, true elsewhere
    transition_seconds    the crossover fade (default 120)
    units: { <unit id>: { mode: auto | off } }      (default auto)
  }
  ```

  **`mode` is the only per-unit setting; the tuning knobs are per section, room-wide.**
  Earlier drafts of these docs called the knobs per-unit. That was a loosening of the
  predecessor's shape, and it is corrected here: a unit's *target* is learned per section,
  but how maintenance hunts for it (the deadband, the step, whether it runs at all near
  the sensor's noise floor at night) is a property of the room's sensor at that time of
  day. `auto` learns; `off` holds the unit's soft switches off for the section (P5.1).

The section vocabulary is core's (`core.config.SECTIONS`, and the schema's `section` enum),
so the config can validate the matrix without importing the module; the adaptive module
takes its sections from there, never the reverse.

**Normalised on every load and save.** `config.apply_defaults` fills the block's defaults
and reshapes `scenes` to exactly the room's current units × the six sections: a missing
section, knob or cell takes its default, and the column of a unit that no longer exists is
dropped. Removing a unit anywhere (the editor, ignoring its last member, a sync that takes
its bulbs out of the room) never leaves an orphan column, and a new unit arrives `auto` in
every section. Only what is absent is filled: anything present stays as written, an explicit
`null` included, so what it doesn't recognise (a misspelt section or knob, a mode that isn't
one) is left in place for validation to report, never silently dropped or fixed.
Unlike `system`'s tuning, the scene defaults are written into the document: the matrix has
to cover every unit and section, and the editor shows exactly what is saved.

**Cross-checks** (core, without HA): `adaptive_bulbs` are active bulbs of the room; the units
partition exactly the adaptive bulbs (no bulb in two units, no switched-only or foreign bulb
in one, no adaptive bulb left out); `scenes` covers every unit × all six sections with `mode`
∈ {`auto`, `off`}, and holds no column for a unit the room doesn't have; `schedule_profile`
resolves; and the sensor ids are well formed (domain, not Light Map's own). `prune_members`
(the reconcile, ignoring) drops a bulb from `adaptive_bulbs` as it drops it from its unit,
which `snapshot_changes` reports through the unit, and reshapes the matrix itself, so what
a sync or an ignore proposes is already a config that saves.

**Dimmability is a UI-time guard only.** The config can't see HA, and its checks must not
depend on it. The editor's read (`GET /api/rooms/{room}/adaptive_config`) reads each active
bulb's state once and classifies it by `supported_color_modes`:

- exactly `{onoff}` → **can't dim**: locked switched-only, can't join a unit, with a note
  saying why. A bulb already adaptive (a hand edit, or saved before HA reported it) is
  shown demoted in the editor's draft, with a note that saving makes it switched-only. The
  demotion is the editor's, not the user's: Save offers it ("not saved yet"), but Revert
  has nothing to undo and leaving doesn't ask about it, since it comes back on every
  opening.
- the attribute missing, or the bulb `unavailable`, `unknown` or absent, or HA unreachable
  → **capability unknown**: not locked, with a soft note. Locking on an outage would lock a
  dimmer out because it happened to be offline.
- anything else → dimmable.

The save never consults HA, and works without it.

**The editor is dedicated and per room.** The Map's "Adaptive config" button opens the room's
editor in the main view (`#config/<room>`), with a way back to the Map. The Config tab
(`#config`) lists the adaptive rooms, each opening the same editor: there is no global
adaptive config. The editor works on a draft: the sensors (entity suggestions come from the
same state read), the schedule profile, each active bulb's tier with the dimmability lock,
the learning-unit partition, and the matrix (a row per section with its knobs, and an
Auto/Off cell per unit). A field holding something invalid keeps what was typed, beside
its error, until it is fixed, and Save waits. It asks before leaving with unsaved edits
(a tab, a link, the browser's Back, closing the page); staying keeps the history the user
moved through, and a save already in flight lands either way. The read never fails on
HA's account: an error, or an answer that isn't a list of states, leaves every capability
unknown. Saving sends the whole
block (`PUT /api/rooms/{room}/adaptive_config`) with the room's `version` as `base`, like
every other edit: one made against an older version is refused (409 `edit_stale`), and the
editor reloads the room as it now is. The save is validate-then-write, and the event log
records it with what changed. It changes only the config.

The editor's `version` (settled in P5.1) is its own: the room's adaptive flag and block, as
the Map's room `version` covers, plus the room's **active-bulb roster** (the ids of its
bulbs that aren't ignored). A bulb ignored, moved out or synced in from another tab while
the editor is open changes which bulbs the editor may offer without always touching the
block (a switched-only bulb's doesn't), and the stale editor's save would then fail
validation (an ignored bulb made adaptive: a 422 the user can't act on) rather than say
the room changed. With the roster in the base it is a clean 409 `edit_stale`, and the
editor reloads. Since P5.2b the `version` also covers the ids of the schedule profiles its
picker offers: a profile can now be deleted (in the schedule editor), and one deleted while
it is selected here made this editor's save a 422 about a missing profile.

**Version 4.** P5.0 bumps the config to version 4. A version-3 adaptive room gets
`adaptive_bulbs` = its bulbs that are in its learning units (what a version-3 unit meant:
those bulbs learn), and everything else defaulted. A version-3 `scenes` was an open object
that nothing wrote but `{}`: it is normalised like any other, and whatever in it doesn't fit
the new shape is left for validation to report. Version 4 also checks two things version 3
let through, which only a hand edit could have set: a sensor's domain (a `device_tracker`
as a presence sensor, an `input_number` as a lux sensor) and a profile's `name` being text.
A config holding one is refused with the problem named, to fix by hand, rather than
migrated by a guess. A room that isn't adaptive has no block to migrate, so a house with no
adaptive room loads exactly as before; the bump is what makes an older Light Map refuse a
version-4 config rather than load an adaptive block it can't read.

---

## The adaptive generator (settled in P5.1)

P5.1 builds everything needed to generate, reconcile and preview the adaptive plane: the
`al_` artefacts (`app.modules.adaptive.generate`), the bridge-brightness fragment, and the
`al_` deploy lane. What *fires* the scene automations (the scheduler and the runtime
crossover loop) and what *writes* the almanac (the analyser and its publish) are P5.2a's
(see "The adaptive engine"); every generated template tolerates the almanac's absence.

### The cutover switch

The adaptive lane keeps the predecessor's `al_` names, and those are the names the live
`ha-adaptive-light` holds on the same HA until cutover. Even the helpers are named alike
(`AL Active <room>`, `AL Hold <room>`, `AL Scene <room>`, as the predecessor names them from
the room id), so an `al_` deploy against the live HA would adopt the old system's helpers,
rewrite its scene and watchdog automations, and remove its `al_maintenance_` ones (the
`al_` prefix is the lane's, and anything in a lane the config doesn't call for is removed).
Light Map must not write `al_` to the live HA before P6.

So the adaptive module is wired in behind one switch, `ADAPTIVE_ENABLED` in the composition
root (`app/main.py`), **off until P6**:

- **Off** (how it runs until cutover): the composition root registers neither the
  adaptive namespace nor its bridge-brightness provider. `core.generate.namespaces()` is
  `["lm"]`, every bridge takes the plain path, and the generated `lm_` lane is
  byte-identical to P5.0's (a test holds it to a golden file captured before P5.1). Nothing
  generates, previews or writes `al_`, and the lifespan makes no adaptive runtime (P5.2a):
  nothing fires a scene, raises a hold, publishes an almanac, or hooks a section or a guard
  into the observer.
- **On** (the tests, and P6): `al` is registered and generated, `core.deploy` reconciles it
  as its own lane, the deploy preview shows its plan (read-only), adaptive bulbs' bridges
  carry the fragment, and the lifespan starts the runtime.

It is a constant, not an environment variable, so turning it on is a reviewed code change
made with the cutover, never a deployment setting someone could set early (in a compose
file or an `.env` copied from a guide). Tests turn it on in a scope of their own
(`app.main.wire_modules(adaptive=True)` with the registries patched), never in the live
composition root. Even on, the Map's deploy applies only the `lm_` lane: applying `al_`
from the Map arrives with the cutover (P6), which removes the old system first.

### The two-plane rework

The predecessor drove bulbs directly: a crossover called `light.turn_on` with a brightness
(which also powered a light on) or `light.turn_off` for a group held off, on HA group
entities. Light Map's adaptive generator keeps its proven shape (guard, hold, scene select,
almanac read, settle delay; maintenance on HA's own clock; the two watchdogs) and reworks
the actuation to the two planes:

- **Power goes through soft switches only.** At a crossover each learning unit's member
  bulbs get the unit's power intent on their soft switches: `input_boolean.turn_on` for a
  unit that is on this section, `input_boolean.turn_off` for a unit held off. The bridge
  makes each bulb follow. Nothing in the `al_` lane calls `light.turn_off`, ever: "off for
  a section" holds the soft off.
- **Brightness is guarded and never powers a bulb on.** A member whose soft switch is
  already on and whose bulb is on gets the new section's brightness pushed directly (its
  soft doesn't change, so its bridge won't re-fire), guarded on both: a brightness command
  on an off bulb would power it on. A member whose soft was off comes on through its
  bridge, at the fragment's brightness. The push runs before the soft switches are turned
  on, so a member the crossover powers on isn't also pushed.
- **Nothing from the almanac is baked.** Not the targets, and not `mode` either: the
  predecessor baked a unit held off as a `light.turn_off`, so that it held from the first
  deploy; Light Map reads it from the almanac like everything else (the P5.2 publish writes
  a held-off unit as `"off"`), so flipping a unit off for a section is a republish, never a
  regeneration. The scene's `transition_seconds` is config, not almanac, and is baked.
- **An absent or partial almanac leaves bulbs as they are.** No sensor, no row for the
  section, no target for a unit: that unit's bulbs are not touched. The learner never
  decides a light should be off; only an explicit `"off"` does.
- Maintenance iterates **bulbs**, not units or groups: each adaptive bulb that is on (soft
  on, bulb on, a brightness above 0) is nudged from its own brightness, brightness only.

### Artefacts (per adaptive room, the `al_` lane)

A room is generated for when it is adaptive (it has an `adaptive_config`), whether or not
any bulb in it is adaptive yet: the guard, hold and scene select are the room's, and the
section it is in is worth recording for the observer. Bulbs come through
`core.inventory.active_bulbs`; an ignored bulb appears in none of it. Output is sorted by
room, unit and bulb id, so it is byte-identical whatever order the config lists them in.

- **Helpers.** `input_boolean.al_active_<room>` (the guard: raised while the lane's own
  automations change the room's lights), `input_boolean.al_hold_<room>` (the maintenance
  hold: the user intervened, stay out for the rest of the section; P5.2 raises it), and
  `input_select.al_scene_<room>` (the section the room is in; its options are the six
  canonical section ids, so its state *is* the section id; custom display names are
  deferred past P5.2b, see "Section display names"). Each is named from the room id (`AL Active <room>`, ...),
  because HA derives a network-created helper's id from its slugified name. The guard has
  `initial: false`, so it is off whenever HA starts: no run survives a restart, and a guard
  restored on would never change, so its watchdog (a state trigger, attached after the
  restore) would never fire. The hold is restored: an intervention stands for the rest of
  the section.
- **The guard is shared, so the last run out lowers it.** A crossover is fired with
  `automation.trigger`, which skips its conditions, so it can start while maintenance is
  mid-run, and two crossovers can overlap. Each run that raises the guard lowers it at its
  end only when none of the room's other guard-raising automations is running (HA's
  `current` attribute on `automation.<id>`); otherwise the first to finish would lower it
  while the other's fade still had minutes to go, and the rest of that fade (which carries
  no context HA links to the run, past five seconds) would be recorded as a person at a
  dimmer. An automation entity under another id reads as idle, which only takes the check
  back to the predecessor's unconditional release.
- **`al_scene_<room>_<section>`**, one per section: `triggers: []` and `conditions: []`
  (fired by P5.2's runtime with `automation.trigger`, which skips conditions), mode
  `single`. It raises the guard, clears the hold, sets the scene select to the section
  (before any soft switch changes: the fragment reads it), reads the section's row of the
  almanac, and for each unit branches on its target: a brightness turns the members' soft
  switches on (after pushing the brightness to members already on), `"off"` turns them
  off, anything else leaves them. Then it waits out the transition plus 30 seconds and
  lowers the guard (last run out). A room with no learning unit changes no light, so it
  skips the wait.
- **`al_maint_<room>`** (the predecessor's `al_maintenance_<room>`), only for a room with
  adaptive bulbs and a lux sensor: every `system.heartbeat_interval_minutes` on HA's own
  clock (`time_pattern`; an hour is `minutes: 0`, since HA allows `/1`..`/59`), unless the
  guard or the hold is raised. It reads the current section from the scene select and that
  section's maintenance inputs from the almanac, compares the room's lux with the target
  and margin, and when out of band raises the guard, nudges every adaptive bulb that is on
  by at most `max_step_pct` of its brightness (1 to 10 steps, floored at 1: maintenance can
  dim a light but never turn one off), waits out the 30-second transition, and lowers the
  guard (last run out). Lux is the mean of the room's sensors reading above 0, as the
  observer takes it; a reading of 0 is the sensor's floor, so with every sensor at 0 (or
  none available) there is no measurement and no nudge, rather than a ramp toward full
  brightness. Whether to nudge is decided in one template, and the step's fraction is read
  from the row where it is used (held to (0, 1]): HA parses each variable's render back to
  a value and keeps one that isn't a plain decimal (`1e-05`, `nan`) as a string, so a
  number passed between variables could turn into text and fail the run after the guard
  is up.
- **`al_watchdog_guard_<room>`**: the guard on for too long (ten minutes, or longer for a
  room whose longest crossover transition needs it) means a run died mid-way, and a stuck
  guard silently stops maintenance and observation: release it and raise a persistent
  notification. It also acts when automations are reloaded: a deploy that rewrites a scene
  or the maintenance while it runs stops that run before it lowers the guard, so a guard
  that no run holds any more is released then and there, quietly (a deploy's routine
  aftermath, and the stuck timer would be lost if the watchdog itself was reloaded).
  **`al_watchdog_stale_<room>`**: the scene select unchanged for ten hours (the longest
  legitimate gap is sleep to sunrise) means the runtime is down: notify. A restart resets
  its timer (a restored select doesn't change), so the runtime's own health after a
  restart is P5.2's to report. There is no watchdog on the hold, which is meant to last the
  rest of a section.

Room names reach a notification's text with their braces turned to parentheses: HA renders
service data as a template, and an Area name is free text.

Each automation's `alias` is its id (so its entity is `automation.<id>`, which is what the
runtime triggers and the guard's release reads), and its description says what it does in
words. That holds for an automation HA registers new; one adopted by an update keeps its old
entity id, so P6 removes the old system's automations before the `al_` lane's first deploy
(each is then created fresh), and P5.2's runtime should find each by its `id` attribute
rather than assume the entity id.

### The almanac contract

`sensor.al_almanac_<room>` is the seam between P5.1 (which reads it) and P5.2 (which
publishes it, over REST, as a state with attributes). The generator reads only these
attributes; anything else the publish adds (`friendly_name`, `mode`, `days_analysed`, ...)
is ignored, and the state itself is never read.

```
attributes.<section>  (one per section id: sunrise, day, afternoon, sunset, night, sleep)
  = {
      units:               { <learning-unit id>: <target> }
      lux_target:          number | null      null: no target yet, no maintenance
      lux_margin:          number             the deadband either side of the target
      max_step_pct:        number in (0, 1]   the largest single nudge
      maintenance_enabled: true | false       only true enables maintenance
    }

<target> = an integer 1..255   on, at this brightness (HA's 0-255 scale)
         | "off"               held off for this section (the unit's mode is off)
         | null, or absent     nothing learned yet: leave the unit's bulbs as they are
```

- A target is the unit's **effective** target for the section: the P5.2 publish resolves
  the config's `mode` into it (`off` publishes `"off"`), so the generated automations never
  read the config's mode.
- The unit targets sit under `units`, not beside the knobs: a unit id is a free slug, and
  one called `lux_target` would otherwise collide with a knob. (The predecessor put group
  ids and knobs in one mapping, and used 0 for off; `"off"` can never be mistaken for a
  brightness, where a 0 pushed as one would turn a light off through a brightness command.)
- **Read defensively, everywhere.** An absent sensor, an absent section, a row or a
  `units` that isn't a mapping, an absent unit key, and any value that isn't one of the
  three forms all read as "nothing learned yet". The scene leaves those bulbs as they are;
  maintenance doesn't run without a row, a numeric `lux_target` and `maintenance_enabled:
  true`; a missing `lux_margin` or `max_step_pct` takes the config's defaults (5, 0.04).

### The bridge fragment

The module registers a bridge-brightness provider (`core.switching`'s seam). For an
adaptive bulb (listed in its adaptive room's `adaptive_bulbs`) it returns gated data
(`GatedBrightness`, settled in P5.2a; see "The soft→bulb bridge"); for every other bulb
(switched-only, in a room that isn't adaptive, in Unassigned) it returns None, and that
bulb's bridge stays on the plain path, byte for byte. Both templates read the current
section from `input_select.al_scene_<room>` and the bulb's unit's target from the almanac,
so a hard switch brings an adaptive bulb up already right for the time of day:

- the gate: does the unit have an on-target (1..255) for the section?
- the data: `{brightness: <that target>}`, applied only when the gate says yes.

Anything else (no almanac, no section yet, nothing learned, `"off"`: a person turning on a
unit held off for the section is a power decision the soft switch owns) takes the plain
`light.turn_on`, and the bulb comes on at its own last brightness, as a switched-only one
does. (Until P5.2a the fragment had to render a brightness on every path, and an off bulb
reports none to keep, so this case came on at 255: at 3 a.m., in a sleep section held off,
at full brightness.) The data still renders without error when there is no target (it is
rendered beside the gate, then not used).

It sets no `transition`: the same bridge serves a hard switch, where a light fading in over
the section's two-minute crossover transition would read as broken. A crossover that powers
a unit on therefore brings its bulbs straight to the target; one that changes the
brightness of bulbs already on fades over the section's transition.

### The `al_` deploy lane

`core.deploy` reconciles `al_` exactly as it does `lm_`, in its own lane (create, update,
remove, verify after every write), with two additions:

- **`input_select` is a helper domain** (`deploy.HELPER_DOMAINS`), for the scene select.
  A lane owns the helper domains its package names: each generator lists every domain it
  writes, empty or not, and deploy reads, plans, verifies and summarises only those. So the
  switching lane still reads and writes input_booleans alone, as before, and an `lm_`
  input_select (which nothing makes) is no business of it.
- **The `al_` prefix is reserved**, as `lm_` is: any UI-managed helper or automation in HA
  whose id starts with `al_` is the lane's, and one the config doesn't call for is removed
  (the predecessor's own `al_maintenance_` ones at cutover, but also, say, a user's
  `input_boolean.al_fresco`). The preview lists every removal before anything is applied;
  at P6, check it for anything that isn't the old system's.
- **A lane checks its references.** A namespace is registered with what its artefacts
  address in HA (`core.generate.register_namespace(..., references=...)`; the switching
  lane's is `switching.references`), and a deploy is refused before writing if any is
  missing. For `al_`: each adaptive bulb, its soft switch (so the switching plane must be
  deployed first), and the room's lux and presence sensors (the deploy-time existence check
  P5.0 promised for them).

The crossover report still counts only the `lm_` lane as Light Map's, so the live old
system's `al_` automations stay flagged as foreign drivers; with the switch on, Light Map's
own `al_` automations are flagged alike, being indistinguishable by name (P6 removes the
old ones).

---

## The adaptive engine (settled in P5.2a)

P5.2a builds what drives the plane P5.1 generated: the schedule profiles and the scheduler,
the analyser, the almanac's store and publish, and the runtime loop. All of it is wired in
by the composition root only with the cutover switch on (see "The cutover switch"); with it
off nothing here runs. The read-only views over it, and the profile editor, are P5.2b's
(see "The adaptive views").

### Schedule profiles

A profile (`schedule_profiles[]`, selected by a room's `adaptive_config.schedule_profile`)
says when each section starts, every day, ported from the predecessor's `schedule`:

```
{ id, name?, collision_policy: collapse | clamp, min_section_minutes (1..720),
  sections: [ { id: <section>, name?, trigger, priority? } x 6, in canonical order ] }

trigger = { type: clock, time: "HH:MM" }
        | { type: sun, event: sunrise | sunset, offset_minutes? }      (-720..720)
        | { type: earliest | latest, of: [trigger, trigger, ...] }     (two or more)
```

- **Priority** decides a collision: higher wins, and by default a clock trigger is 100 and
  a sun one 50 (a fixed time states a fact about the household's routine; a sun-relative
  one an assumption about daylight that the collision has just falsified). A composite is
  as firm as its firmest component. Ties go to the section earlier in canonical order.
- **Collisions.** A section conflicts with those already kept for the day in one of two
  ways: *order* (a section later in canonical order has already started: a UK midsummer
  sunset at 21:21 after night at 20:30) or *gap* (it starts within `min_section_minutes`
  of the last, too soon for the crossover's transition). The loser is **collapsed**,
  skipped for the day with its reason. Under `clamp` a gap conflict instead moves the later
  section to start `min_section_minutes` after the one before, as long as that is still
  within the day; one that can't fit collides as under `collapse`, and an order inversion
  always collapses, since moving sunset later still leaves it after night.
- A sun event that doesn't happen that day (polar day or night) collapses its section; an
  offset that pushes a boundary out of the day is clamped to 00:00..23:59. So every
  section that runs starts within its own day.
- A section's `name` is accepted (the predecessor's profiles had one) and is the boundary's
  name. Nothing shows or sets it yet: display names are deferred (see "Section display
  names"), and the P5.2b editor keeps one a profile already holds as written.
  `min_section_minutes` is at least 1: two sections starting at one instant would leave one
  never in force.
- **The standard schedule** (the predecessor's): sunrise = the earlier of sunrise and
  05:30 (priority 100), day = sunrise + 3 h, afternoon = sunset − 3 h, sunset = sunset
  (each 50), night = 20:30, sleep = 22:00 (each 100); `collapse`, 30 minutes.

**Version 5.** P5.2a bumps the config to version 5. A version-4 profile was an id and a
name that nothing read, so every one, `default` included, is backfilled with the standard
schedule, keeping its id and name; anything already in it stays as written, for
validation to report if it doesn't fit. `collision_policy` and `min_section_minutes` are
filled with their defaults on every load when absent, but a version-5 profile must list its
sections: there is no safe guess at someone's day. The cross-check refuses sections that
aren't the six, once each, in canonical order. `learning` is fixed at the same time (all
optional, defaults in code; see `docs/ARCHITECTURE.md`): version 4 had it as an open object
that nothing wrote but `{}`. The bump is what makes an older Light Map refuse a version-5
config.

### The scheduler

`app.modules.adaptive.scheduler` is the predecessor's, ported intact: pure computation (a
profile, a date and a place in, the day's six boundaries out, each run or collapsed with
its reason), testable offline across a whole year, which matters because its failure modes
are seasonal. Two departures, each a day the predecessor got wrong (found by P5.2a's
review): its clamp could move a section past midnight, even after the next day's first
boundary, where it was reported as run but never in force (now it collapses instead, as
above); and it compared times by the wall clock, so where the clocks go back across a
boundary (Beirut, at midnight) the repeated hour read as before the boundary again, which a
runtime polling it would answer by firing the previous section a second time (now it
compares instants). Sun times are computed locally (`astral`) from HA's own location (latitude,
longitude, elevation and time zone, from `/api/config`): HA's `sun.sun` only knows the next
event, so it can't say when today's sunrise was, which catching up after a restart needs.

It is **level-triggered**: `active_section_at` says which section is in force at a moment
(looking back up to two days, since before the day's first boundary the last section of
yesterday is still in force), and the runtime fires a crossover whenever that differs from
what it last fired. Normal running, a start after downtime, a dropped connection and a
clock change all take the same path.

`tools/sweep.py` runs a year of days at five latitudes (the equator to Tromsø) for each
profile, listing every collapse with its dates and reasons, and fails on an *unexpected*
one: a section neither run nor collapsed with a reason, kept boundaries out of
chronological or canonical order, two kept sections closer than the minimum, or one
starting outside its day (the predecessor's clamp).

### The analyser

`app.modules.adaptive.analyser` is the predecessor's learning pipeline with the maths
unchanged: the recency ladder, the confidence bands and their multipliers, the reactive
weight, the post-reactive boost, the rolling weighted average, and the bootstrap and
learning states (with the publish delay only once learning). Its `AnalyserFlags` are the
predecessor's three, at its defaults, so it reproduces the predecessor exactly; every
behavioural difference lives behind a flag. The tuning comes from the config's `learning`.

**The one rework: per bulb, pooled per learning unit.** The store keeps readings per bulb;
the analyser pools a unit's members at analysis time (`pool_unit`): a sample holds one
reading per adaptive bulb, stability is judged per bulb, and a unit's daily brightness is
the weighted mean over all its members' eligible readings together (Σ brightness × weight /
Σ weight, across members and samples), its on-fraction likewise. A unit of one bulb is
exactly the predecessor's per-group computation, which the tests hold it to: they run the
predecessor's analyser (`tests/reference/`, verbatim) and this one on the same readings,
under every combination of the flags, and require the same almanac.

What the predecessor's store recorded that this one doesn't is derived, the same way:

- **The day** a sample belongs to is its timestamp's date in HA's time zone (the runtime
  passes the time in it), so a sleep section that crosses midnight splits across two days,
  as before.
- **Occupied** is a heartbeat whose occupancy is 1, or null in a room that had no presence
  sensor configured (the predecessor: "no sensor means always occupied"). A configured
  sensor with no usable state read as unoccupied there, and is excluded here. Which of the
  two a null meant is recorded with the heartbeat (`presence_configured`, P5.2a), never
  taken from the config at analysis time: judged by today's config, adding a sensor turned
  every earlier heartbeat into "unoccupied", and the room fell back to bootstrap on a day of
  data. A heartbeat recorded before that column existed is judged by the config as it is.
- **Any light on** means any adaptive bulb of the room: a switched-only bulb is outside the
  learner entirely (its readings never pool into a unit, and it neither opens a sample to
  the lux target nor settles one).
- A reading with no usable state (`is_on` null) is no reading for that bulb: the predecessor
  could only record it as off. A heartbeat or reactive event with no section (recorded
  before the runtime ran) is skipped. A reactive event counts when it changed an adaptive
  bulb; one that changed nothing the learner learns is the predecessor's "phantom".

`run(store, config, now=...)` analyses every adaptive room and returns the new snapshots:
one row per unit per section, learned or not (see the storage model's `almanac`), and none
for a room with no data in the lookback. The learned brightness is kept whatever the
unit's mode: whether a unit is held off is the config's, resolved at publish, so turning it
back to auto finds its target waiting. `tools/compare_analyser.py` runs it twice over the
same input, legacy flags against the current, and prints both, from a copy of a config's
archive (with HA's time zone, `--tz`, which files each reading under its day) or from the
predecessor's wide heartbeat CSV.

### The almanac

`app.modules.adaptive.almanac` records the snapshots (insert-only, through
`store.append_almanac`), reads the one in force (`latest`: per unit and section, the newest
row whose `valid_from` has come, local date), and publishes it (`publish`) as
`sensor.al_almanac_<room>` in exactly the contract above:

- per section, `units` holds every current unit's target: `"off"` when the config holds the
  unit off for the section (whether or not anything is learned), else its learned
  brightness (1..255), else null;
- `lux_target` is the learned one, else the section's `lux_target_seed` when above 0 (0 lux
  is a sensor's floor: a target of 0 can't be met, and maintenance would dim every light
  toward 1 chasing it), else null;
  `lux_margin`, `max_step_pct` and `maintenance_enabled` are the config's scene matrix as
  it is now, not as it was at the last pass, so a knob changed in the editor takes effect at
  the next publish;
- the state is the `valid_from` of the snapshot in force (`unknown` before the first),
  so a stalled analyser shows as a date that stops moving; `friendly_name`, `mode` and
  `days_analysed` ride along as attributes the automations ignore.

### The runtime

`app.modules.adaptive.runtime.AdaptiveRuntime` is thinner than the predecessor's loop,
which also did the observing: core's observer does the heartbeats and the reactive
detection, and the runtime plugs into it.

- **Start.** It registers the section provider (the section it last fired for each
  adaptive room, before its first crossover the scheduled one), the guard provider
  (`input_boolean.al_active_<room>`, so a heartbeat waits out a crossover or a nudge and
  their late transition steps are never learned) and its observation handler, then runs
  its loop. Once HA answers it reads HA's location, and **adopts** each room's section from
  its scene select: a room whose select already shows the section in force isn't fired
  again, because re-firing the current scene at every restart would turn back on the soft
  switches the user has turned off since. A room is adopted only once its select is there
  showing a section; until then (HA still starting, whose REST API answers before every
  integration has loaded, or the `al_` lane not deployed) nothing fires for it. A reconnect
  (HA restarting) makes it read the location and adopt again. Each scene automation is
  found by its `id` attribute, not an assumed entity id.
- **Every 20 seconds** (`tick`) it works out each adaptive room's section from the clock and
  its profile, and when it differs from the one fired, fires the room's
  `al_scene_<room>_<section>` with `automation.trigger`, and sets its scene select once
  the scene has run. The scene sets the select itself before it changes any light, so a
  select showing a section means its scene started: setting it first, a restart between
  that and a failed trigger's retry would adopt the section and never run its scene. The
  trigger runs in the background: HA answers it only when the run, its transition and
  settle included, is over. One HA didn't take is retried at the next tick (logged once);
  one HA took but didn't answer in time is running (HA shields it from a dropped request)
  and isn't retried, since a second run could turn back on what the user turned off since.
  With no scene automation in HA, only the select is set (so the bridges and maintenance
  read the right section), logged, and the room waits for its next section. A collapsed
  section is logged once a day.
- **Nightly**, at 00:15 local, or at the first tick after if it was down then (a pass
  already in the store for the day counts), it runs the analyser in a worker thread and
  records the snapshots.
- **The publish is level-triggered too**: every tick it builds each adaptive room's
  almanac (the snapshot in force, with the config) and publishes it when it differs from
  what it last published. A unit turned off in the editor, a snapshot coming into force at
  midnight, a new pass: each is a republish and nothing else is. A state set over REST
  doesn't survive an HA restart, so a reconnect forgets what was published and the next
  tick publishes everything again.
- **A reactive event** in an adaptive room raises `input_boolean.al_hold_<room>`:
  maintenance stands down for the rest of the section, and the next crossover's scene
  lowers it. Not for an event whose window opened in the section before (a dimmer turned in
  a section's last two minutes closes after the next scene lowered the hold, and would keep
  maintenance out of a section the user never touched), and not once the runtime is
  stopping. Any bulb of the room counts, a switched-only one too: maintenance holds the
  room's lux, so it would dim the adaptive bulbs to undo a lamp the user just turned up. The
  runtime never records or reclassifies an observation; that is core's.

It only commands HA (services, the almanac's state) and reads what it can't get from the
observer (the location, and the scene selects and automation ids once); it never reads a
bulb or a sensor itself.

---

## The adaptive views (settled in P5.2b)

P5.2b puts a face on the engine: three read-only tabs (Now, Analysis, Almanac) and the
schedule-profile editor. It adds no engine behaviour, no generated artefact and nothing that
writes HA: the generated lanes are as P5.2a left them (the `lm_` lane still byte-identical to
its golden file), and the one thing it writes is the config's `schedule_profiles`, through
the editor's validate-then-write save. Each view is per adaptive room (`#now/<room>`,
`#analysis/<room>`, `#almanac/<room>`), with the adaptive rooms to pick from at the top; the
room picked is kept while moving between the three.

### Reading beside the live system

Until cutover the live `ha-adaptive-light` owns the `al_` entities on the same HA:
`input_select.al_scene_<room>`, `input_boolean.al_active_<room>` and `al_hold_<room>`, and
`sensor.al_almanac_<room>`. A view that read them would show the old system's state as if it
were Light Map's. So the views draw on three sources, and only the last is gated:

- **The config and HA's location** (`/api/config`: latitude, longitude, elevation, time
  zone): the schedule, and the time zone a day is counted in. Safe with the switch either
  way.
- **Light Map's own store**: the observations, and the almanac snapshots the nightly pass
  records (`almanac.latest`). Safe either way: until the runtime runs (P6) it holds no
  snapshot, and the views say "nothing learned yet". The Almanac tab reads the store, never
  `sensor.al_almanac_<room>`, which is the old system's until P6 (and once Light Map
  publishes it, the store is still what it is published from).
- **The live `al_` entities** (the scene select, the guard, the hold) and the runtime's
  status: only with the module wired in (`app.main.adaptive_wired_in()`, which follows the
  cutover switch). Off, the Now tab shows "Adaptive runs at cutover — not live yet" in their
  place, and its endpoint reads no HA state at all: its one HA call is the location
  (`get_config`). On, it reads HA's states once and takes the room's three `al_` entities
  from them.

The tabs are shown, not hidden, with the switch off: the schedule a room would follow and
whatever the store holds are worth seeing before cutover, and the Now tab is how a profile
is checked before it drives anything.

### Now

`GET /api/rooms/{room}/now`. First the live state, or with the switch off the not-live note
in its place, so the tab says at once whether adaptive is driving the room. Then today's
schedule: the room's profile computed for today at HA's
location (`scheduler.compute_day`, times in HA's time zone, as `HH:MM`), each section with
its start and end, its trigger and effective priority, and whether it runs, was moved (a
`clamp`) or is skipped today and why. The section in force now is highlighted, with a NOW
badge as well as colour (`scheduler.active_section_at`; before the day's first boundary it
is yesterday's last section, and the tab says so). Above it, the profile's name, the other
rooms that share it, and a link to edit it. Below, the current section's unit targets as the
store's almanac would publish them now (`almanac.payload`: `off` for a unit held off, the
learned brightness, or nothing learned yet) and its lux target. Without HA's location there is no schedule (a sun-relative boundary needs
the place), and the tab says why rather than guess.

### Analysis

`GET /api/rooms/{room}/analysis` runs `analyser.analyse_room` on demand, as the nightly pass
would for the room now (its current learning units, the config's `learning`, the lookback
ending today), in a worker thread, and returns the rolling almanac it computes. It records
nothing: the store's queries run read-only (`Store.query`), and nothing writes the snapshot.
Per section: each unit's learned brightness and on-fraction, the room's lux target, the days
that contributed, how many were high-confidence, and the trust weight behind it (the
accumulated recency × confidence × settled weight); overall, bootstrap or learning (with how
many of the bootstrap minimum's days are in), the days analysed and the date a snapshot
from it would take effect. A room with no learning unit has nothing to analyse, and says so.

Two charts, one measure each across the six sections (canonical order, the order of the
day): each unit's learned brightness (0–255), and under it the room's lux target. Never one
chart with two y-axes: how two scales line up is arbitrary, and the eye reads a
correlation into it. They are uPlot, vendored (`static/vendor/uplot/`, as the predecessor
did: one self-contained file, no CDN, no build step). Series are told apart by more than
colour: each unit's line has its own dash pattern, the legend draws each line as it is
drawn, up to four lines are labelled at their end (where the labels don't collide), and a
table under the charts gives every value. Pointing at a chart, or focusing it and using the
arrow keys, reads out every series at that section. The unit colours are eight fixed slots
in the theme's CSS tokens (`--series-1..8`, checked against the panel for colour-blind
separation and contrast), assigned in order and never repeated: a ninth unit is in the
table, not on the chart. Days are counted in HA's time zone; without it, in UTC, and the
tab says so.

### Almanac

`GET /api/rooms/{room}/almanac`: the snapshot in force (`almanac.latest`, the newest per unit
and section whose `valid_from` has come) with the date it took effect, when its pass ran,
its mode and days analysed. Per section, each unit's effective target exactly as the publish
resolves it (`almanac.payload`) beside the learned brightness behind it and the date that
row took effect, and the section's lux target (learned, else the seed above 0) and knobs as
the publish reads them (the config's, as it is now). History the store keeps for units the
room no longer has is named under the table (their ids), not shown in it.

### The schedule editor

The global `schedule_profiles`, at `#schedule` (one profile at a time, `#schedule/<id>`),
reached from the Config tab beside the adaptive rooms, from the room editor's profile picker
and from the Now tab. It works on a draft of the whole list, as the room editor does on its
block: create (a copy of the profile on screen, or the standard schedule), rename, reorder
(the list's order is the order the room editor offers them in), and delete a profile no room
selects, never `default` (the schema requires it). Per profile: its collision policy and
minimum section length; per section, its trigger (a clock time; sunrise or sunset with an
offset; the earliest or latest of two or more of those) and an optional priority (blank:
the trigger's default, shown). The six sections are always there, in canonical order: the
cross-check requires it, and the order they start in on a given day is the scheduler's to
work out. Profile ids are slugs fixed at creation from the name; renaming keeps the id,
since rooms select a profile by id.

**A profile is shared.** Every room that selects it follows it, so the editor names the
rooms that use the profile on screen and warns that saving changes each of them.

- `GET /api/schedule_profiles`: the list, its `version`, the adaptive rooms and the profile
  each selects, and each profile's day today at HA's location (when it can be read).
- `POST /api/schedule_profiles/check`: a draft list in; what saving it would refuse (the
  same validation the save runs) and each profile's day today out. It writes nothing; the
  editor sends it as the draft changes, so a collision shows as it is made.
- `PUT /api/schedule_profiles`: the whole list, against the `version` it was shown
  (`base`; 409 `edit_stale`), validate-then-write, recorded in the event log with what
  changed. Config only: nothing reaches HA, and the runtime reads a profile at its next tick
  once it runs (P6). Refused (409 `sync_first`) before the first sync has saved a config,
  as creating a room is: a save would create a config holding no rooms, and the Map would
  then take the house as synced.

The check and the days run in a worker thread, off the event loop, since the editor sends a
draft every time it changes. A trigger nested hundreds of levels deep (`earliest` within
`earliest`, over and over: the schema's grammar sets no depth) is refused as too deep to
check, by the config's own load (`config.TOO_DEEP`), rather than exhausting the recursion
limit inside the schema validator.

The `version` covers the profiles and which profile each adaptive room selects. The editor
shows the rooms a profile is shared with, and offers to delete a profile no room uses: a
room switched to a profile in another tab while the editor is open must make its save a
409 that reloads it, not a deletion refused only by the cross-check (a 422 saying a reference
broke) or a warning that names the wrong rooms.

### Section display names (deferred)

P5.2b doesn't give sections display names. The scene select's options are the canonical
section ids (P5.1), and every generated template reads its state as the section id, so a
custom name needs the templates to map it back to its id: a generator change, and P5.2b
makes none. The views label sections by their canonical names, and the editor neither shows
nor sets a section's `name` (one already in a profile is kept as written).

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
  configured or available; `presence_configured` (P5.2a) says which of the two a null is,
  as it was when recorded. Presence sensors are read as binary sensors (`on` is
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
- **Learning unit** — a set of adaptive bulbs the learner pools into one brightness target.
  The almanac key.
- **Adaptive / switched-only** — the two tiers of an active bulb in an adaptive room: an
  adaptive bulb learns and follows the scenes; a switched-only one keeps the plain
  switching plane (on at its last brightness).
- **Section** — one of the six fixed parts of the day (`sunrise`, `day`, `afternoon`,
  `sunset`, `night`, `sleep`) the scene matrix and the almanac are keyed by.

---

## Side effect: Presence Rules nearly free

The two-plane split makes a future Presence Rules module nearly free: presence simply commands
the soft switch, like any other input to the switching plane.

---

## Open items

- **Old-data re-ingest at cutover** — start the almanac fresh vs re-ingest the predecessor's
  per-group CSV where a group maps cleanly to one bulb.
- **Per-bulb detail** (colour temperature) — deferred. Dimmability is settled (P5.0, "The
  adaptive editor"): a bulb HA reports as on/off only is switched-only even in an adaptive
  room, enforced by the editor; the config doesn't record it.
- **Out-of-band power changes** — a bulb switched outside the plane (HA's UI, a vendor app,
  voice, or the live `ha-adaptive-light` during the parallel build) leaves its soft switch
  stale: the bridge only follows the soft, never the reverse. The next toggle may then
  first "turn off" a bulb that is already off. Whether to reflect a bulb's power back into
  its soft switch (and how, without that change re-triggering the bridge) is open; settle
  it before cutover.
- **Section display names** (deferred from P5.2b) — the scene select's options are the
  canonical section ids (P5.1), and every template reads its state as the section id.
  Custom display names (the predecessor's) need the templates to map a name back to its
  id, a generator change P5.2b doesn't make (see "Section display names").
