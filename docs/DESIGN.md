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
3. **Soft switches are generated.** Each bulb automatically gets an `input_boolean`. Group
   soft switches are *not* auto-generated — the user adds them per room, picks member bulbs
   and the physical switch, and can reopen to edit.
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
  - `bulbs[]`: `{ id, name, entity_id (light.*), hard_switches[] }` — a soft-switch helper is
    implicit (generated) per bulb.
  - `switching_groups[]`: `{ id, name, members (bulb ids), hard_switches[] }` — a soft-switch
    helper is implicit (generated) per group, modelled as an HA group of its members' soft
    switches.
  - `hard_switches[]`: `{ id, name, source (entity_id or event/device ref), mode }`, `mode` ∈
    `toggle` | `follow` | `press`.
  - `adaptive_config` (only when `adaptive`): `{ lux_sensors[], presence_sensors[],
    schedule_profile, learning_units[], scenes }`. `learning_units[]` =
    `{ id, name, members (bulb ids) }`; `scenes` is the per-section matrix keyed by
    learning-unit id × section (per-unit, per-section `mode` auto/off, plus lux margin,
    maintenance flag, transition, max step).

The existing single room from `ha-adaptive-light` is re-expressed against this shape rather
than migrated; history starts fresh (as the n8n→container cutover did).

---

## Generated artefacts

A parallel namespace to the adaptive `al_*` artefacts, deployed and reconciled the same way
but in its own lane:

- `input_boolean.lm_soft_<id>` — one per bulb and per switching group.
- A group's soft switch is a native HA group of its members' soft switches, so group state is
  *derived* (on if any member is on) with no hand-written state logic.
- `lm_hard_<id>` (hard→soft): each hard switch's trigger acts on its soft switch (toggle /
  follow / press — and a one-way "all off" is a `press` that turns the group soft off rather
  than toggling it).
- `lm_bridge_<id>` (soft→bulb): soft on → turn the bulb(s) on and apply brightness; soft off
  → turn off.
- The adaptive almanac (`sensor.al_almanac_<room>`) is keyed by **learning-unit id**.

No template engine; byte-deterministic output; no baked-in almanac state (the bridge reads
the almanac at runtime for an adaptive bulb's brightness).

---

## The soft→bulb bridge (where the planes meet)

The bridge is the only thing that powers a bulb on or off. On `soft → on` it turns the bulb
on; for an adaptive bulb it applies the current section's learned brightness (the bulb's
learning-unit target), for a plain bulb a configured default (or last brightness). On
`soft → off` it turns the bulb off. So flipping any hard switch brings an adaptive light up
already-correct for the time of day.

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

## Hard-switch modes

Per binding: `toggle` (any state change flips the soft — decoupled relays, or dumb switches
wired to a relay in detached mode), `press` (a stateless scene button/remote firing an event
or device trigger acts on the soft — a one-way "all off" is a `press` that turns a group soft
off), and `follow` (soft tracks the hard switch's on/off directly). A soft switch may carry
several hard switches of mixed modes.

---

## Terminology

- **Bulb** — one `light.*` entity; the actuation atom.
- **Switching group** (UI: "group soft switch") — a set of bulbs toggled together by a hard
  switch. Keeps the word "group".
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
