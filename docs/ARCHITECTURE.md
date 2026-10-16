# Light Map — Architecture

Light Map is a self-hosted container that owns a home's lighting as two planes: a
**switching plane** (every bulb has a soft switch; hard switches bind to it; a soft→bulb
bridge makes the bulb follow) and **modules** that layer behaviour on top. **Adaptive
Light** — learning a preferred light level per section and driving brightness — is the first
module. This repository is a fresh, ground-up rebuild of `ha-adaptive-light`, map-first.

This document is the structural source of truth: system layout, module boundaries, the
config shape, the storage model, naming, the house rules, and the build roadmap. The
reasoning behind the design, and the detailed data model, live in `docs/DESIGN.md`.

---

## The two planes

Adaptive Light (the old system) did two jobs through one action: at a crossover it decided
both *whether* a light was on and *how bright*. Light Map splits those:

- **Switching plane (core).** The soft switch (`input_boolean`) is the single source of
  truth for a light's power. Hard switches, modules, and the user all act on the soft
  switch, never the bulb directly. A soft→bulb bridge makes the bulb follow its soft switch.
- **Modules.** Behaviour layered on top, acting only by commanding soft switches (power) and
  setting bulb brightness (guarded on soft-on). Adaptive Light owns *brightness when on* for
  the rooms flagged adaptive. Presence Rules is the natural second module.

The inventory — rooms (from Home Assistant Areas), bulbs, switching groups, soft/hard
switches — is core. Modules attach per room. A room never flagged for any module still gets
the full switching plane; the map is useful on its own.

---

## System architecture

Two tiers: a **core/platform** and **modules**.

**Core (the trunk):**

- *HA transport* — REST + WebSocket, plus Area/entity-registry read and write. Knows nothing
  about sections or almanacs.
- *Inventory* — discover every `light.*`; rooms from HA Areas (two-way, with a committed
  snapshot for determinism); bulbs; user-defined switching groups.
- *Switching plane* — generate the soft-switch helpers and the hard→soft and soft→bulb
  bridge automations; reconcile them.
- *Observation* — the live WebSocket observer: per-bulb heartbeats, reactive (manual-change)
  detection, the event log. A platform service any module can consume.
- *Config / generate / deploy* — validate-then-write config; deterministic artefact
  generation per namespace; deploy that creates/updates/removes helpers, automations, and
  Area assignments.
- *UI shell + Map tab* — the inventory editor and the deploy surface.

**Adaptive Light module:**

- sections and time profiles; the level-triggered scheduler and collision policy; the
  analyser; the almanac (keyed by **learning unit**); the scene and maintenance automation
  generation (commanding soft switches, reading learning-unit targets); the Now / Analysis /
  Almanac views and the adaptive room editor.

### Module interface (what makes a module a module)

A module plugs into the core through four seams, so Adaptive Light is replaceable and
Presence Rules can be added without touching the trunk:

1. **Per-room config block.** A module contributes a typed section under a room (Adaptive's
   is `adaptive_config`), present only when the room enables it.
2. **Generated artefacts in its own namespace.** The module emits HA helpers/automations
   under a reserved prefix; deploy reconciles each namespace independently.
3. **Observation subscription.** The module reads the platform's per-bulb heartbeats /
   reactive events; it never scrapes HA itself.
4. **Actuation vocabulary.** A module acts only by commanding soft switches (power) and
   setting bulb brightness guarded on soft-on — never by powering bulbs directly. This is
   the invariant that keeps the switching plane the single source of truth for power.

---

## Repository layout

Stub files declare their responsibility in the module docstring; later phases fill them in.

```
app/
  __init__.py
  main.py            Composition root: the FastAPI app wiring core + modules, plus HTTP
                     endpoints. The entry point; `python -c "import app.main"` must succeed.
  core/
    __init__.py
    ha.py            Transport: HA REST + WebSocket + Area/entity-registry read & write.
                     Knows nothing about rooms, sections, or almanacs.
    inventory.py     Discover every light.* entity; build rooms from HA Areas; manage
                     switching groups; keep the committed bulb→room snapshot in sync with HA
                     (read Areas to refresh, write back on edit).
    switching.py     Generate the switching-plane artefacts: per-bulb/group soft-switch
                     helpers, hard→soft automations, soft→bulb bridge automations.
    observe.py       The live observer: per-bulb heartbeats, reactive (manual-change)
                     detection, the event log. A platform service modules subscribe to.
                     Readings are per bulb, long format.
    config.py        Load/validate/default the config against the JSON Schema; cross-check
                     references; reconcile the Area snapshot. Validate-then-write: a config
                     that won't load must never replace one that does.
    store.py         CSV archive (source of truth) + SQLite query layer (rebuildable). Event
                     log. CSV re-ingest. No state that lives only in SQLite.
    generate.py      Namespace registry + deterministic YAML assembly. NO template engine:
                     assemble Python data structures, serialise with PyYAML; HA Jinja stays
                     an inert string. Same config → byte-identical output.
    deploy.py        Reconcile helpers/automations/Area writes against live HA across
                     namespaces; create/update/remove; verify after write. Each namespace
                     stays in its own lane.
  modules/
    __init__.py
    adaptive/
      __init__.py
      scheduler.py   Compute section boundaries (sun-relative + fixed) and apply collision
                     policy. Ported from ha-adaptive-light.
      analyser.py    Nightly learning pipeline. Pools per-bulb observations per LEARNING
                     UNIT into one target. Ported; behavioural changes gated behind flags.
      almanac.py     The learned model, keyed by learning unit; pushed to HA over REST.
      generate.py    Scene + maintenance + watchdog automations. Commands SOFT SWITCHES for
                     power; reads learning-unit targets for brightness. conditions: [] on
                     scene automations.
      runtime.py     The adaptive loop: subscribe to core observation, run the scheduler,
                     fire crossovers, push the almanac.
static/
  index.html         Tab shell: Map, Now, Analysis, Almanac, Config, Log.
  app.css
  app.js             (per-view JS/CSS split arrives in later phases)
schema/
  config.schema.json The config contract (see "Top-level config shape").
  storage.schema.sql SQLite DDL (see "Storage model").
tools/
  doctor.py          Probe a real HA instance; validate connectivity and entities; print the
                     rooms the inventory would build. Read-only.
  sweep.py           A year of section-boundary computations, checking for unexpected
                     collapses.
tests/               Unit tests (stdlib unittest, `python -m unittest`) against fakes and
                     fixtures; never a real HA.
Dockerfile
docker-compose.yml
.env.example
requirements.txt
CLAUDE.md
docs/
  ARCHITECTURE.md
  DESIGN.md
  ROADMAP.md
README.md
```

---

## House rules (invariants)

These hold across the whole system; `CLAUDE.md` restates them for contributors. Several are
ported from `ha-adaptive-light` and were hard-won — preserve them.

- **CSV is the archival source of truth; SQLite is a rebuildable query layer.** Never add
  state that only lives in SQLite; it must always be reconstructable from the CSV archive.
- **No template engine in the generators.** Generated automations embed HA Jinja verbatim;
  assemble Python data structures and let PyYAML serialise them. Rendering Jinja to produce
  Jinja makes the outer pass consume the inner one.
- **Deterministic generation.** The same config must always produce byte-identical YAML.
  Avoid dict/set ordering, timestamps, or other nondeterminism leaking into output.
- **Entity IDs only, never device IDs.** Device IDs are opaque, break on re-pairing, and
  can't be validated by an entity-existence check.
- **The soft switch owns power.** The soft→bulb bridge is the only thing that powers a bulb
  on or off. Modules act only by commanding soft switches and setting brightness, and every
  brightness command is guarded on "soft is on" so it can never power a bulb on.
- **Nothing from the almanac is hardcoded into generated YAML.** The automation reads the
  almanac at runtime and branches; flipping a unit to "off" for a section needs only a
  republish, not regeneration.
- **Scene automations carry `conditions: []`.** They are fired via `automation.trigger`
  (default `skip_condition: true`); a condition placed there would be silently ignored.
- **Reactive detection ignores the system's own changes.** A brightness change counts as a
  user intervention only when it carries no `context.parent_id` and no guard is raised.
  Switch-driven and bridge-driven changes carry a `parent_id`, so toggling power is never
  learned as a brightness preference.
- **Observation is per bulb, long format**, pooled per learning unit at analysis time — so
  learning units can be redrawn without losing history.
- **HA Areas are the source of truth for room membership.** The config holds a committed
  snapshot (with the `ha_area_id`); registry writes are verified after the fact and
  reconcilable, never destructive by surprise.
- **The WebSocket read loop must start before subscribing.** Subscribing first deadlocks
  until timeout and leaves the event stream dead while the log still says "connected."
- **Config writes are validate-then-write**, never the reverse.
- **The HA token is never persisted in the config** — stripped on save/export, read from the
  environment or a separate secrets file at runtime.
- **Comments explain *why*,** especially around past bugs: leave the failure mode, not just
  the fix.

---

## Top-level config shape

Everything downstream is a pure function of this committed document. Learned data (almanacs)
is not stored here; it lives in the almanac store. Detailed field semantics are in
`docs/DESIGN.md` ("Data model").

- `version` — schema version; the container migrates forward on load.
- `homeassistant` — `base_url`, `access_token` (redacted from exports; never persisted),
  `verify_ssl`; reconnect backoff arrives with the event subscription's reconnect loop (P4).
  At runtime the connection is read from the environment / secrets: `LM_HA_BASE_URL`,
  `LM_HA_TOKEN` (or `LM_HA_TOKEN_FILE`). `LM_HA_BASE_URL`, when set, overrides `base_url`
  (settled in P1): the environment belongs to the deployment, the config to the house. The
  token comes only from the environment; one found in the config file is ignored.
- `system` — observation cadence, reactive window/min-delta, data dir, CSV toggle, event-log
  retention.
- `rooms[]` — from HA Areas (every Area is a room, with or without lights), each:
  - `id` (slug), `name`, `ha_area_id`, `adaptive` (bool)
  - `bulbs[]`: `{ id (slug), name, entity_id (light.*), hard_switches[] }` — a soft-switch
    helper is implicit (generated) per bulb.
  - `switching_groups[]`: `{ id (slug), name, members (bulb ids), hard_switches[] }` — a
    soft-switch helper is implicit (generated) per group, modelled as an HA group of its
    members' soft switches.
  - `adaptive_config` — the per-module block, **required when `adaptive` is true** and
    forbidden when false: `{ lux_sensors[], presence_sensors[], schedule_profile,
    learning_units[], scenes }`, where `learning_units[]` = `{ id (slug), name,
    members (bulb ids) }` and `scenes` is the per-section matrix keyed by learning-unit id ×
    section.

  `hard_switches[]` is the binding shape used **inside `bulbs[]` and `switching_groups[]`** —
  it is not a key on the room itself: `{ id, name, source (entity_id or event/device ref),
  mode }`, `mode` ∈ `toggle` | `follow` | `press`. A room-wide action (e.g. an "all off"
  button) is a hard switch bound to a switching group that covers the room, not a room-level
  binding.
- `unassigned[]` — the Unassigned bucket: bulbs (same shape as `rooms[].bulbs[]`) whose light
  has no HA Area. Kept in the snapshot so a bulb's id and hard switches survive while it has
  no room. Not a room: no switching groups, no module config.
- `schedule_profiles[]` — reusable time-section schedules (adaptive module). A profile with
  id `default` must always exist.
- `learning` — adaptive analyser tuning.

**Id scopes** follow the HA names the ids end up in. Room ids are global
(`sensor.al_almanac_<room>`), and no two rooms share an HA Area. Bulb ids and switching-group
ids share one global namespace, because both name a soft switch (`input_boolean.lm_soft_<id>`)
— including the bulbs in `unassigned[]`. Hard-switch ids are global (`lm_hard_<id>`).
Learning-unit ids are unique within their room. Schedule-profile ids are global. The schema
checks shape; `app/core/config.py` cross-checks these scopes and every reference.

As of P1 the core sections (`homeassistant`, `rooms[]`, `unassigned[]`) are validated in
depth; `system`, `learning`, `schedule_profiles` and `adaptive_config` fill in with the
observer (P4) and the adaptive module (P5).

---

## Storage model

CSV is the archive; SQLite is the rebuildable query layer. Readings are stored **per bulb**
(long format), which is what learning-unit pooling needs. The almanac is insert-only
snapshots keyed by learning unit, so history comes for free. This is the starting skeleton;
exact columns are finalised when `store.py` and the analyser are ported (P4/P5).

Tables (skeleton):

- `heartbeat` — one row per observation tick: `id`, `ts`, `room_id`, `section`, `lux`
  (nullable; 0 is a floor, not a measurement), `occupancy` (bool).
- `bulb_reading` — long format, one row per bulb per heartbeat: `id`, `heartbeat_id` (fk),
  `bulb_id`, `brightness` (nullable), `is_on` (bool).
- `reactive_event` — a consolidated manual change: `id`, `ts`, `room_id`, `section`,
  `bulb_id`, `brightness_before`, `brightness_after`, `weight` (avoid `before`/`after` —
  they are SQL keywords).
- `almanac` — insert-only learned snapshots: `id`, `ts`, `room_id`, `section`,
  `learning_unit_id`, `lux_target`, `lux_margin`, `maintenance_enabled`, `max_step_pct`,
  `trust_weight`, `valid_from`, `mode`.
- `event_log` — `id`, `ts`, `level`, `category`, `room_id` (nullable), `message`,
  `detail` (json).

---

## Naming and namespaces

- **Product / container:** Light Map. Repo `ha-light-map`.
- **Switching plane:** `input_boolean.lm_soft_<id>` (per bulb and per group); automations
  `lm_bridge_<id>` (soft→bulb) and `lm_hard_<id>` (hard→soft); a group's soft switch is an HA
  group of its members' soft switches.
- **Adaptive module:** keeps the recognizable `al_` names (`al_scene_`, `al_maint_`,
  `al_active_`, `al_hold_`, `sensor.al_almanac_`, `al_watchdog_`), so the ported generator
  barely changes.
- Deploy reconciles `lm_*`/`lm_soft_*` and `al_*` as **separate lanes** — the switching
  deployer never touches `al_*`, and vice versa.

---

## What ports, what's new, what's reworked

The rebuild is clean structure, not re-derived internals. The hard-won algorithms port
across as module internals.

- **Ports ~intact:** `scheduler.py` (boundary + collision), `analyser.py` (pipeline + its
  flag-gated diffing), the reactive/guard/hold logic, the three learning states, the
  CSV-archive/SQLite-query split, `tools/`.
- **New:** inventory + rooms-from-Areas + snapshot sync; Area/registry read-write in
  `ha.py`; the switching-plane generator (soft/hard/bridge); the Map tab; the module
  interface and registry; per-room multi-module deploy/reconcile.
- **Reworked:** `generate.py` splits into core (switching) and the adaptive generator, with
  adaptive now commanding soft switches and reading learning-unit targets; `analyser.py`
  pools per bulb into learning units; the almanac re-keys to learning units; `runtime.py`
  becomes a platform observer plus an adaptive loop; `config.py` gains the map schema and
  Area reconcile; `deploy.py` gains Area writes and multi-namespace reconcile.

---

## Roadmap

Parallel build in this repo; the live `ha-adaptive-light` keeps running untouched until P6.

- **P0 — Scaffold.** Repo structure, stub modules, schema skeleton, docs, container/compose,
  validation-harness stubs. No behaviour, no HA contact.
- **P1 — Inventory + Areas.** Discover `light.*`; rooms from Areas; two-way Area sync with
  the committed snapshot; config loader/validation. Read-only against HA plus Area writes.
- **P2 — Switching plane.** Soft-switch generation; hard→soft and soft→bulb automations;
  deploy/reconcile including Area writes. Switching works end-to-end with no adaptive.
- **P3 — Map UI.** The Map tab: rooms, the adaptive checkbox, per-bulb soft-switch popups,
  group-soft ADD/edit popups, the Adaptive Config button (inert until P5).
- **P4 — Observation core.** The WebSocket observer, per-bulb heartbeats, reactive detection,
  the event log, storage. A platform service.
- **P5 — Adaptive module.** Port scheduler + analyser; learning units; almanac keyed by
  unit; scene + maintenance generation commanding soft switches; the Now / Analysis /
  Almanac views and the adaptive room editor.
- **P6 — Cutover.** Deploy against live HA; remove the old `al_*` system; re-express the
  house; start the almanac (fresh or re-ingested); decommission `ha-adaptive-light`. Staged,
  with the old repo kept deployable as rollback until Light Map has run clean for a few days.

P0–P4 touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. That coexistence boundary is the main
thing to get right during the parallel build.

---

## Open decisions

- **Adaptive artefact prefix** — keep `al_` (recommended) or fold under `lm_al_`.
- **Old-data re-ingest at cutover** — start the almanac fresh vs re-ingest the existing
  per-group CSV where a group maps cleanly to one bulb.
- **Adaptive-config revisions** — the planned adjustments to the old Config view are not yet
  specified; they shape P5.
- **Per-bulb detail** (dimmable, colour-temp) — deferred; a non-dimmable bulb is
  switching-only even in an adaptive room.
- **Module-interface formality** — how far to formalise the module seams now vs a clean
  internal boundary with adaptive as the only module until Presence Rules lands.

---

## References

- `docs/DESIGN.md` — the reasoning, the three-layer model, the bridge mechanics, learning
  units, and the detailed data model.
- `ha-adaptive-light` (the predecessor) — `docs/DESIGN.md`, `docs/ROADMAP.md`, `CLAUDE.md`,
  `schema/` — the source of the proven pieces being ported.
