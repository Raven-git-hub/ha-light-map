# Light Map — Architecture

Light Map is a self-hosted container that owns a home's lighting as two planes: a
**switching plane** (every bulb has a soft switch, bar the lights the user ignores; hard
switches bind to it; a soft→bulb bridge makes the bulb follow) and **modules** that layer
behaviour on top. **Adaptive Light** — learning a preferred light level per section and
driving brightness — is the first module. This repository is a fresh, ground-up rebuild of
`ha-adaptive-light`, map-first.

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
   Part of this seam is the **bridge brightness**: the soft→bulb bridge is core, but a
   module may supply, per bulb it owns, the `light.turn_on` data the bridge applies on
   soft-on (`brightness`, `brightness_pct`, `transition`; HA Jinja allowed, kept inert).
   It registers a provider with `core.switching.register_bridge_brightness`; the core
   generator embeds the fragment in that bulb's `lm_bridge_` automation, inside the
   soft-on branch, so it is guarded on soft-on by construction and can't retarget the
   bridge or touch power. Adaptive's almanac read lives in that fragment (P5), not in core.
   A bulb no module claims takes the plain path (its last brightness). A provider may also
   **gate** its data (P5.2a, `core.switching.GatedBrightness`): a condition template under
   which the bridge applies it, falling back to the plain `light.turn_on` otherwise, so a
   bulb the module has nothing for comes on at its own last brightness. The gate only
   chooses which of two `light.turn_on`s on the bridge's own target runs; both are inside
   the soft-on branch.

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
    switching.py     Generate the switching-plane artefacts: per-bulb soft-switch helpers,
                     hard→soft automations, soft→bulb bridge automations; the
                     bridge-brightness seam modules fill.
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
                     policy. Ported from ha-adaptive-light (P5.2a).
      analyser.py    Nightly learning pipeline. Pools per-bulb observations per LEARNING
                     UNIT into one target. Ported; behavioural changes gated behind flags
                     (P5.2a).
      almanac.py     The learned model, keyed by learning unit: insert-only snapshots in
                     the store, the current one per unit and section, and its publish to
                     HA over REST in the attribute contract (P5.2a).
      generate.py    Scene + maintenance + watchdog automations. Commands SOFT SWITCHES for
                     power; reads learning-unit targets for brightness. conditions: [] on
                     scene automations.
      runtime.py     The adaptive loop: hook into core observation (the section, the guard,
                     the hold), run the scheduler, fire crossovers, run the nightly
                     analysis, publish the almanac (P5.2a).
static/              Vanilla JS and CSS, no build step; desktop-oriented, dark.
  index.html         Tab shell: Map, Now, Analysis, Almanac, Config, Log.
  app.css            The theme and the components every view shares.
  app.js             The shell: tab router (`#view/arg`), API helper, DOM/notice/dialog
                     helpers, the leave guard for unsaved edits.
  map.css, map.js    The Map tab (P3). Each later view adds its own pair.
  log.css, log.js    The Log tab (P4): the event log, read-only.
  config.css,        The Config tab (P5.0): the adaptive rooms, and the per-room adaptive
  config.js          editor (`#config/<room>`), opened from the Map.
  adaptive.css,      The Now, Analysis and Almanac tabs (P5.2b): read-only, per adaptive
  adaptive.js        room (`#now/<room>`, ...), sharing the room picker and the chart.
  schedule.css,      The schedule-profile editor (P5.2b, `#schedule/<profile>`), under the
  schedule.js        Config tab.
  vendor/uplot/      uPlot 1.6.31 (MIT), the Analysis chart: vendored as published, one
                     self-contained file and its stylesheet, no CDN, no build step.
schema/
  config.schema.json The config contract (see "Top-level config shape").
  storage.schema.sql SQLite DDL (see "Storage model").
tools/
  doctor.py          Probe a real HA instance; validate connectivity and entities; print the
                     rooms the inventory would build (and, given a config, today's section
                     boundaries at HA's location). Read-only.
  sweep.py           A year of section-boundary computations at several latitudes,
                     reporting each collapse and checking for unexpected ones (P5.2a).
  compare_analyser.py  Run the analyser twice over identical input (legacy vs current
                     flags) and diff the almanacs; reads a copy of a config's archive, or
                     the predecessor's wide heartbeat CSV (P5.2a).
tests/               Unit tests (stdlib unittest, `python -m unittest`) against fakes and
                     fixtures; never a real HA. `test_ui.py` also drives the Map, the Log
                     and the adaptive editor in headless Chromium (`tests/ui/map_ui.cjs`,
                     `log_ui.cjs`, `config_ui.cjs`, `lanes_ui.cjs`, Node's Playwright) where
                     that is installed, and is skipped where it isn't (P5.2b adds `adaptive_ui.cjs`, the Now /
                     Analysis / Almanac tabs, and `schedule_ui.cjs`, the schedule editor). `ha_script.py` renders
                     the generated Jinja and runs the generated automations against a fake
                     state machine (P5.1; needs jinja2, from `requirements-dev.txt`, else
                     those tests skip). `golden/` holds byte-exact generated output: `lm_only.yaml`
                     (captured before P5.1: the cutover switch off must reproduce it) and
                     `al_committed.yaml`. `reference/` holds the predecessor's analyser,
                     verbatim, as the oracle the analyser's parity tests run against (P5.2a).
Dockerfile
docker-compose.yml
.env.example
requirements.txt
requirements-dev.txt   Test-only: jinja2, for the tests that render the generated templates.
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
  can't be validated by an entity-existence check. A bulb's `ha_match_key` (its HA
  entity-registry entry id) is an opaque HA id the config holds as a match key only: the
  snapshot reconcile matches on it, and an Area write compares it with the registry entry
  at the bulb's `entity_id` so it never moves a different light (P3). It is never
  addressed, required to exist in HA, or written into generated artefacts. Anything that
  addresses a bulb uses its `entity_id`. **One carved-out
  exception:** a hard switch's `device` source names a `device_id`, only to *read* a button
  press from a device that exposes no entity or event to bind instead. That id appears
  only in the trigger of its `lm_hard_` automation; it never addresses a light or targets
  an action.
- **The soft switch owns power.** The soft→bulb bridge is the only thing that powers a bulb
  on or off. Modules act only by commanding soft switches and setting brightness, and every
  brightness command is guarded on "soft is on" so it can never power a bulb on.
- **Nothing from the almanac is hardcoded into generated YAML.** The automation reads the
  almanac at runtime and branches; flipping a unit to "off" for a section needs only a
  republish, not regeneration.
- **Scene automations carry `conditions: []`.** They are fired via `automation.trigger`
  (default `skip_condition: true`); a condition placed there would be silently ignored.
- **Reactive detection ignores the system's own changes.** A brightness change counts as a
  user intervention only when it carries no `context.parent_id`, wasn't made under an
  automation run's own context, and no guard is raised. Switch-driven and bridge-driven
  changes carry a `parent_id`, so toggling power is never learned as a brightness
  preference. An automation fired by time or the sun carries no `parent_id` (HA starts it
  with a fresh context), which is why the observer also watches `automation_triggered`.
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

- `version` — schema version (5 since P5.2a, which fixed `schedule_profiles` and
  `learning`; 4 since P5.0, which fixed `adaptive_config`; 3 since P4.1, which added
  `ignored`; 2 since P2); the container migrates forward on load.
- `homeassistant` — `base_url`, `access_token` (redacted from exports; never persisted),
  `verify_ssl`, `reconnect_backoff_seconds` (the WebSocket reconnect ladder, default
  `[1, 2, 5, 10, 30, 60]`; the last step repeats).
  At runtime the connection is read from the environment / secrets: `LM_HA_BASE_URL`,
  `LM_HA_TOKEN` (or `LM_HA_TOKEN_FILE`). `LM_HA_BASE_URL`, when set, overrides `base_url`
  (settled in P1): the environment belongs to the deployment, the config to the house. The
  token comes only from the environment; one found in the config file is ignored. The
  config file itself lives at `LM_CONFIG_PATH` (default `/data/config.json`, on the
  container's volume); before its first save there is none, and the app starts from a blank
  config.
- `system` — the observer's settings (P4), each optional with a default:
  `heartbeat_interval_minutes` (10), `reactive_window_seconds` (120), `reactive_min_delta`
  (5, in HA's 0–255 brightness), `event_log_retention_days` (90), and `data_dir` (where the
  archive and the query layer live; default, and base for a relative path, the directory
  holding the config). There is no CSV toggle: the CSV archive is the source of truth, so
  it is always written (see "Storage model"). `data_dir` is read at startup.
- `rooms[]` — from HA Areas (every Area is a room, with or without lights), each:
  - `id` (slug), `name`, `ha_area_id`, `adaptive` (bool)
  - `bulbs[]`: `{ id (slug), name, entity_id (light.*), ha_match_key?, ignored,
    hard_switches[] }` — a soft-switch helper and a soft→bulb bridge are implicit
    (generated) per bulb that isn't ignored. `ha_match_key` is the light's HA
    entity-registry entry id: an opaque key the reconcile matches on, so a bulb survives
    its entity_id being renamed in HA (see `docs/DESIGN.md`, "Bulb identity"). `ignored`
    (default false; P4.1) sets aside a light that isn't a room bulb (an indicator or gadget
    LED): it stays in the snapshot, so it can be un-ignored, but generation, deploy, the
    Area writes and the observer leave it out, it is no group's or learning unit's member
    and has no hard switches. Never set automatically (see `docs/DESIGN.md`, "Ignored
    lights").
  - `switching_groups[]`: `{ id (slug), name, members (bulb ids), hard_switches[] }` — a
    named set of member bulbs plus its hard-switch bindings. A group mints **no** soft
    switch of its own: a hard switch bound to it acts on every member's per-bulb soft
    switch.
  - `adaptive_config` — the per-module block, **required when `adaptive` is true** and
    forbidden when false (P5.0; see `docs/DESIGN.md`, "The adaptive editor"):
    - `lux_sensors[]` (`sensor.*`), `presence_sensors[]` (`binary_sensor.*` or
      `input_boolean.*`): the room's sensors.
    - `schedule_profile`: the id of a `schedule_profiles[]` entry (default `default`).
    - `adaptive_bulbs[]` (bulb ids): the room's adaptive bulbs. Adaptive-ness lives here,
      not on the bulb; every other active bulb of the room is switched-only (the plain
      plane: on at its last brightness).
    - `learning_units[]` = `{ id (slug), name, members (bulb ids) }`: a partition of
      exactly the adaptive bulbs.
    - `scenes`: the matrix over the six sections (`sunrise`, `day`, `afternoon`, `sunset`,
      `night`, `sleep`): `scenes.<section>` = `{ lux_target_seed (null = observe),
      lux_margin, max_step_pct, maintenance_enabled, transition_seconds, units: { <unit
      id>: { mode: auto | off } } }`. The knobs are per section, room-wide; `mode` is the
      only per-unit setting. Normalised on load to exactly the room's units × the six
      sections (defaults filled, a removed unit's column dropped).

  `hard_switches[]` is the binding shape used **inside `bulbs[]` and `switching_groups[]`** —
  it is not a key on the room itself: `{ id, name, source, mode, turn? }`, `mode` ∈
  `toggle` | `follow` | `press`, and `turn` ∈ `on` | `off` (required for `press`, forbidden
  otherwise). `source` is a tagged union: `{ type: "state", entity_id }` (a stateful
  switch / binary_sensor / light / event entity), `{ type: "event", event_type,
  event_data? }` (a stateless button firing an HA event), or `{ type: "device", device_id,
  trigger }` (a device automation trigger, only for a button that exposes nothing else).
  `follow` needs a `state` source. A room-wide action (e.g. an "all off" button) is a
  `press` with `turn: off` bound to a switching group that covers the room, not a
  room-level binding. Semantics are in `docs/DESIGN.md`, "Hard switches".
- `unassigned[]` — the Unassigned bucket: bulbs (same shape as `rooms[].bulbs[]`) whose light
  has no HA Area. Kept in the snapshot so a bulb's id and hard switches survive while it has
  no room. Not a room: no switching groups, no module config.
- `schedule_profiles[]` — reusable time-section schedules (adaptive module), each `{ id
  (slug), name?, sections[], collision_policy, min_section_minutes }` (P5.2a). A profile
  with id `default` must always exist.
  - `sections[]`: all six sections, once each, in canonical order (`sunrise` ... `sleep`),
    each `{ id (section), name?, trigger, priority? }` (`name`: a display name, accepted
    and kept, not yet shown: deferred past P5.2b). `trigger` says when the section starts
    each day: `{ type: clock, time: "HH:MM" }`, `{ type: sun, event: sunrise | sunset,
    offset_minutes? }`, or `{ type: earliest | latest, of: [trigger, ...] }` (at least
    two; "sunrise or 05:30, whichever is earlier"). `priority` (0–1000) decides a
    collision; by default 100 for a clock trigger, 50 for a sun one, and a composite's
    firmest component's.
  - `collision_policy`: `collapse` (default: the lower-priority section is skipped that
    day) or `clamp` (a section that starts too soon after the last is moved to start
    `min_section_minutes` after it, within its day; one that can't fit, and an order
    inversion, still collapse).
  - `min_section_minutes` (1..720, default 30): the shortest section; one shorter collides.

  Version 5 backfills every profile that has no `sections` with the standard schedule
  (the predecessor's six boundaries: sunrise = the earlier of sunrise and 05:30, day =
  sunrise + 3 h, afternoon = sunset − 3 h, sunset, night 20:30, sleep 22:00; see
  `docs/DESIGN.md`, "Schedule profiles"). The editor arrived with P5.2b (`#schedule`; see
  "HTTP surface"); display names for the sections are deferred (see "Open decisions").
- `learning` — adaptive analyser tuning (P5.2a), every field optional, defaults in code
  (not written into the document): `lookback_days` (21), `publish_delay_days` (2),
  `bootstrap_min_days` (7), `reactive_weight` (5), `post_reactive_boost` ([3, 2, 1.5]),
  `recency_weights` ([{max_age_days: 3, weight: 3}, {7, 2}, {14, 1}, {null, 0.5}]), and
  `confidence` (`high_weight_threshold` 30, `medium_weight_threshold` 10,
  `high_multiplier` 1.5, `medium_multiplier` 1.0, `low_multiplier` 0.5): the
  predecessor's, which reproduce its results.

**Id scopes** follow the HA names the ids end up in. Room ids are global
(`sensor.al_almanac_<room>`), and no two rooms share an HA Area. Bulb ids are global, because
each names a soft switch and a bridge (`input_boolean.lm_soft_<id>`, `lm_bridge_<id>`) —
including the bulbs in `unassigned[]`. Switching-group ids share that one namespace: a
group names no HA entity, but a hard switch binds to a bulb or a group, and keeping the two
apart lets either be named by id alone. Hard-switch ids are global (`lm_hard_<id>`).
Learning-unit ids are unique within their room. Schedule-profile ids are global. A bulb's
`ha_match_key`, when present, is unique across all bulbs. An ignored bulb keeps its id
reserved like any other. The schema checks shape;
`app/core/config.py` cross-checks these scopes and every reference.

The cross-check refuses an ignored bulb that is a switching-group or learning-unit member,
or that carries hard switches: with no soft switch, none of them would act on anything. It
also refuses a hard switch wired into a feedback loop: a source in Light Map's own `lm_`
namespace, a `toggle` whose source is a bulb in the snapshot (an ignored one included:
un-ignored, it would loop), or an event source on an HA core event (`state_changed`,
`call_service`, `automation_reloaded`, ...), which fires for the plane's own work. Deploy
adds the one check that needs HA's state: no `toggle` on an HA group of bulbs Light Map
drives.

For an adaptive room it checks that `adaptive_bulbs` are active bulbs of the room, that the
learning units partition exactly the adaptive bulbs, that `scenes` covers every unit × all
six sections with `mode` ∈ {`auto`, `off`} and no column for a unit the room doesn't have,
that `schedule_profile` resolves, and that the sensor ids are well formed (their domain,
and not Light Map's own `lm_`/`al_` entities). Whether a bulb can dim is not a config
check: the config can't see HA, so the adaptive editor enforces it, from HA's
`supported_color_modes`. The six sections are core's vocabulary (`core.config.SECTIONS`
and the schema's `section` enum), so core validates the matrix without importing the
module.

For each schedule profile it checks that `sections` lists the six sections once each, in
canonical order (the schema checks each trigger's shape).

As of P5.2a every part of the document is validated in depth: the core sections
(`homeassistant`, `system`, `rooms[]`, `unassigned[]`), `adaptive_config` (P5.0), and
`schedule_profiles` and `learning` (P5.2a).

---

## Storage model

CSV is the archive; SQLite is the rebuildable query layer. Readings are stored **per bulb**
(long format), which is what learning-unit pooling needs. The almanac is insert-only
snapshots keyed by learning unit, so history comes for free. The observation tables were
finalised with the store (P4); the almanac's columns are finalised with the analyser (P5).

Tables (`schema/storage.schema.sql`; `ts` is ISO-8601 UTC, booleans 0/1):

- `heartbeat` — one row per room per observation tick: `id`, `ts`, `room_id`, `section`
  (nullable: recorded at observation time from the module that has sections, P5),
  `lux` (nullable; 0 is a floor, not a measurement), `lux_sensors` (how many sensors `lux`
  came from), `occupancy` (nullable: no presence sensor, or none available),
  `deferred_ms` (how long the tick waited for a guard), `presence_configured` (whether the
  room had a presence sensor configured, so a null occupancy says "no sensor" or "no
  usable state" as of then; null for heartbeats recorded before P5.2a). Unique on
  (`room_id`, `ts`).
- `bulb_reading` — long format, one row per bulb per heartbeat: `id`, `heartbeat_id` (fk),
  `bulb_id`, `is_on` (nullable: HA had no usable state for the bulb), `brightness`
  (nullable; 0–255).
- `reactive_event` — one consolidated manual change in a room (one reactive window):
  `id`, `ts` (the window's start), `room_id`, `section`, `window_seconds`, `lux_before`,
  `lux_after`, `occupancy`. Unique on (`room_id`, `ts`).
- `reactive_change` — long format, one row per bulb of the room for each reactive event:
  `id`, `reactive_event_id` (fk), `bulb_id`, `is_on_before`, `is_on_after`,
  `brightness_before`, `brightness_after` (not `before`/`after`: SQL keywords; for a bulb
  the user changed, after is where their last change left it), `changed` (the user changed
  this bulb by more than the min-delta), `user_id` (the HA user behind that change; null
  for a device). There is no stored weight: weighting is the analyser's (P5), applied at
  analysis time, so retuning it reweights history.
- `almanac` — insert-only learned snapshots, one row per learning unit per section per
  nightly pass (finalised in P5.2a): `id`, `ts` (when the pass ran), `room_id`, `section`,
  `learning_unit_id`, `brightness` (the unit's learned brightness, 0–255; null: nothing
  learned), `on_fraction` (how much of the section its bulbs were on), `lux_target` (the
  room's learned lux for the section, the same on every unit's row), `lux_margin`,
  `maintenance_enabled`, `max_step_pct` (the scene matrix's knobs when the pass ran, for
  the record: the publish reads the config's current ones), `trust_weight`,
  `days_contributing`, `high_confidence_days`, `days_analysed`, `valid_from` (the local
  date the snapshot takes effect) and `mode` (the analyser's state: `bootstrap` or
  `learning`). A pass writes every unit × section, learned or not, so the newest row in
  force for a unit and section always comes from the newest pass in force; the unit's
  `auto`/`off` mode is not stored here: the publish reads it from the config.
- `event_log` — `id`, `ts`, `level` (`debug` | `info` | `warning` | `error`), `category`,
  `room_id` (nullable), `message`, `detail` (json).

Each table has a CSV stream under `<data_dir>/archive/<table>/<YYYY-MM>.csv` (by the row's
UTC month) with the table's columns minus the surrogate ids; a child row carries its
parent's natural key (`room_id`, `ts`) instead of its id, so the archive needs nothing from
SQLite to be read back. Every write goes to the CSV first, flushed to disk. The query
layer (`<data_dir>/light_map.db`) records each archive file's size and mtime as of the last
write it holds all of, and is rebuilt from the archive at startup when that doesn't match
(deleted, behind after a failed write, an archive file edited), recreated when its schema
version differs, and moved aside and rebuilt when it is broken. Event-log retention prunes
the query layer only; the archive keeps everything.

---

## Naming and namespaces

- **Product / container:** Light Map. Repo `ha-light-map`.
- **Switching plane:** `input_boolean.lm_soft_<bulb_id>`, one per bulb (that isn't ignored)
  and only per bulb; automations `lm_bridge_<bulb_id>` (soft→bulb) and
  `lm_hard_<hard_switch_id>` (hard→soft). A switching group has no HA entity: an HA group of
  `input_boolean`s can only be made in `configuration.yaml`, which Light Map never edits,
  and with no group entity there is no group state to keep in sync. A hard switch bound to a
  group acts on every member's soft switch. (A single group-state entity for dashboards, if
  ever wanted, would be an HA light group of the member bulbs — a future enhancement, not a
  soft switch.) The soft switch's helper is named `LM Soft <bulb_id>`, because HA derives a
  network-created helper's id from its slugified name.
- **Adaptive module:** keeps the recognizable `al_` names, so the ported generator barely
  changes: per adaptive room the helpers `input_boolean.al_active_<room>` (the guard),
  `input_boolean.al_hold_<room>` (the maintenance hold) and `input_select.al_scene_<room>`
  (the section the room is in), the automations `al_scene_<room>_<section>` (one per
  section), `al_maint_<room>` (the predecessor's `al_maintenance_<room>`),
  `al_watchdog_guard_<room>` and `al_watchdog_stale_<room>`, and the almanac
  `sensor.al_almanac_<room>`, which P5.2 publishes and nothing deploys (its attribute
  contract is in `docs/DESIGN.md`, "The almanac contract"). These are the names the live
  `ha-adaptive-light` uses, which is why the lane stays behind the cutover switch (see
  "Roadmap").
- Deploy reconciles `lm_*`/`lm_soft_*` and `al_*` as **separate lanes** — the switching
  deployer never touches `al_*`, and vice versa. A prefix is reserved by registering its
  generator (`core.generate.register_namespace`); a name belongs to the longest registered
  prefix it starts with, so even an adaptive lane folded under `lm_al_` would stay its own.
  A lane owns the helper domains its package names (`input_boolean` for `lm`; also
  `input_select` for `al`, P5.1), and is registered with what its artefacts address in HA,
  which a deploy checks before writing.

---

## HTTP surface (P3, P4, P5.0, P5.1, P5.2b)

`app/main.py` serves the UI's API under `/api/` (JSON; an error is `{error, detail}`,
with `problems` when there are several) beside `/healthz` and the static UI. The HA client
is made in the app's lifespan from the environment; without a token the Map still serves
the committed config and every HA action says why it can't run. The lifespan also opens
the store and starts the observer (P4), which opens the WebSocket once the config has a
room with bulbs to observe, and, only with the cutover switch on, the adaptive runtime
(P5.2a).

| | |
| --- | --- |
| Reads | `GET /api/status`, `/api/map`, `/api/config` (export, token-free), `/api/entities` (hard-switch source picker), `/api/log` (the event log, filtered by minimum level, category and room; P4), `/api/rooms/{room}/adaptive_config` (the adaptive editor's view: the block, each active bulb's tier, unit and dimmability; P5.0), `/api/rooms/{room}/now`, `/analysis`, `/almanac` (the adaptive views; P5.2b), `/api/schedule_profiles` (the schedule editor's view; P5.2b), `POST /api/schedule_profiles/check` (a draft's problems and its day today; writes nothing) |
| Config edits | `PUT /api/rooms/{room}/adaptive`, `PUT /api/rooms/{room}/adaptive_config` (P5.0), `PUT /api/schedule_profiles` (P5.2b), `PUT /api/bulbs/{bulb}/hard_switches`, `PUT /api/bulbs/{bulb}/ignored` (P4.1), `POST /api/rooms/{room}/groups`, `PUT`/`DELETE /api/rooms/{room}/groups/{group}` |
| Area edits (HA first) | `POST /api/rooms`, `PUT /api/rooms/{room}/name`, `POST /api/bulbs/{bulb}/move/preview` then `/move` |
| Previewed actions | `GET /api/sync/preview` then `POST /api/sync`; `GET /api/deploy/preview` then `POST /api/deploy`; `GET /api/areas/preview` then `POST /api/areas/deploy` |

- **Preview, then apply what was shown.** A preview writes nothing and returns a
  `fingerprint` of what it showed; the apply must carry it, recomputes from fresh state, and
  is refused (409 `preview_stale`) if the result digests differently. So a deploy, a sync,
  an Area write or a move is never applied unless it was previewed as it now stands.
- **Validate-then-write**, every edit; a move also checks its config before its HA write.
  A config on disk that doesn't load is never overwritten from the Map.
- **Edits carry what they were made against.** Each edit of an existing bulb's hard switches
  or ignored flag, group, or room's adaptive flag or adaptive config sends the `version` the
  view showed as `base`, and is refused (409 `edit_stale`) if it has changed since, so a
  stale tab never silently reverts another edit, and an acknowledgement covers only what was
  shown.
- **The adaptive editor's read** reads HA's states once, for each active bulb's dimmability
  (`supported_color_modes`); without HA it still answers, every capability unknown. Its
  save never needs HA. Nothing in P5.0 writes HA. The editor's `version` also covers the
  room's active-bulb roster (P5.1), so a bulb ignored in another tab while it is open makes
  its save a 409 `edit_stale`, not a validation error.
- **Module lanes in the deploy preview** (P5.1). With the cutover switch on, the deploy
  preview also lists each module lane's plan (`modules`), read-only: `POST /api/deploy`
  still applies the `lm_` lane alone, and applying `al_` from the Map arrives with the
  cutover (P6). With the switch off (until P6) there is no such key, and `/api/status`
  says which it is (`adaptive`). With it on, the status also reports the adaptive runtime
  (`adaptive_runtime`, P5.2a): each room's section, the last nightly pass, and what it
  last published.
- **The adaptive views read beside the live system** (P5.2b). The Now, Analysis and
  Almanac tabs take the schedule from the config and HA's location, and the learned data
  from Light Map's own store (the Almanac tab never reads `sensor.al_almanac_`). A live
  `al_` entity (the scene select, the guard, the hold), which the old system owns until
  P6, is read only with the cutover switch on; off, the Now tab says adaptive isn't live
  yet, and makes no call that reads an HA state. Nothing they do writes HA; the analysis
  runs on demand and records nothing.
- **The schedule editor** (P5.2b) saves the whole `schedule_profiles` list against a
  `version` covering the profiles and which profile each adaptive room selects (409
  `edit_stale` otherwise), validate-then-write, config only. It keeps `default`, deletes
  only a profile no room selects, and warns that a profile is shared by every room that
  selects it. See `docs/DESIGN.md`, "The adaptive views".
- **A Map edit commits only its own change** to the snapshot; whatever else HA changed waits
  for a reviewed sync.
- **Coexistence.** An edit that makes a hard switch drive something new is refused (409
  `coexistence`) until the request acknowledges it; the Map warns before saving one and
  marks every live bulb and group.
- The switching deploy and the Area writes are separate actions, each previewed.
- **Host allowlist** (P4). A request whose `Host` header isn't on the allowlist is refused
  (400) before it reaches any route, which closes the DNS-rebinding path (a page on another
  site, re-pointed at the container's address, would otherwise be same-origin with the API).
  The list is `localhost` and `127.0.0.1` plus the names in `LM_ALLOWED_HOSTS`
  (comma-separated: the names or addresses you browse to Light Map by). It is not
  authentication: anyone who can reach the port and send the right name can still drive the
  API (see "Open decisions").
- The actions that change HA or the config are recorded in the event log (P4).

The details are in `docs/DESIGN.md`, "The Map and its API" and "Observation".

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
  the event log, storage. A platform service. **P4.1** adds ignored lights: a per-bulb
  flag that sets an indicator or gadget LED aside from the plane.
- **P5 — Adaptive module.** Port scheduler + analyser; learning units; almanac keyed by
  unit; scene + maintenance generation commanding soft switches; the Now / Analysis /
  Almanac views and the adaptive room editor. In three steps:
  - **P5.0 — Adaptive config + editor.** The `adaptive_config` shape (adaptive /
    switched-only tiers, learning units, the section × unit scene matrix), config version
    4, its cross-checks, and the dedicated per-room editor with the dimmability guard.
    Generates and deploys nothing.
  - **P5.1 — Adaptive generator.** The `al_` lane (scene, maintenance, watchdog), the
    bridge-brightness provider, the almanac attribute contract, and the `al_` deploy lane
    (`input_select` as a helper domain, per-lane reference checks), all behind the cutover
    switch, which stays off.
  - **P5.2a — Adaptive engine.** The scheduler port (schedule-profile internals, config
    version 5, `tools/sweep.py`), the analyser (pooling per learning unit,
    `tools/compare_analyser.py`), the almanac's store and publish, the runtime loop, and
    the bridge's gated fallback (an adaptive bulb with no target comes on at its own last
    brightness), all behind the cutover switch, which stays off.
  - **P5.2b — Adaptive views.** The Now / Analysis / Almanac tabs (read-only, reading a
    live `al_` entity only with the cutover switch on) and the schedule-profile editor,
    with uPlot vendored for the Analysis chart. No engine or generator change; the
    sections' display names are deferred (they need one).
- **P6 — Cutover.** Deploy against live HA; remove the old `al_*` system; turn the cutover
  switch on and add the `al_` lane's apply to the Map's deploy; re-express the house; start
  the almanac (fresh or re-ingested); decommission `ha-adaptive-light`. Staged, with the old
  repo kept deployable as rollback until Light Map has run clean for a few days.

P0–P5.2 touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. (P5.0 to P5.2 add none to the live
HA: the adaptive module they build stays behind the cutover switch.) That coexistence boundary is the main
thing to get right during the parallel build.

**The cutover switch** (P5.1). The adaptive lane's `al_` names are the ones the live
`ha-adaptive-light` uses on the same HA, down to its helpers' names, so an `al_` deploy
before cutover would adopt its helpers and rewrite or remove its automations. The adaptive
module is therefore wired in by the composition root only when `ADAPTIVE_ENABLED`
(`app/main.py`) is on, and it is **off until P6**: `core.generate.namespaces()` is `["lm"]`,
no bridge-brightness provider is registered, the generated `lm_` lane is byte-identical to
P5.0's, nothing generates, previews or writes `al_`, and no adaptive runtime is made (P5.2a):
nothing fires a scene, raises a hold, registers a section or guard with the observer, or
publishes an almanac. It is a constant rather than an
environment variable so that turning it on is a reviewed change made with the cutover,
never a setting flipped early. Tests turn it on in their own scope. Deploying the plane never drives a bulb: each
soft switch is set to its bulb's current on/off before its bridge goes live, and a bridge
fires only on its soft switch changing between `off` and `on`. Binding a hard switch in
the config is the step that makes a bridge authoritative, so leave `hard_switches` empty
(or bind only switches the live system doesn't use) until cutover. The Map warns before
saving one (the API refuses it unacknowledged), marks every live bulb and group, and the
deploy preview lists the hard switches it puts live and the automations (the live
system's `al_` ones among them) that already drive the same bulbs.

---

## Open decisions

- **Adaptive artefact prefix** — keep `al_` (recommended, and what P5.1 builds) or fold
  under `lm_al_`. Folding would let the lane deploy beside the live system before
  cutover, at the cost of renaming every name the predecessor's tooling and dashboards know.
- **Old-data re-ingest at cutover** — start the almanac fresh vs re-ingest the existing
  per-group CSV where a group maps cleanly to one bulb.
- **Per-bulb detail** (colour temperature) — deferred. Dimmability is settled (P5.0): a
  bulb HA reports as on/off only (`supported_color_modes` exactly `onoff`) is switched-only
  even in an adaptive room, enforced by the adaptive editor rather than the config (which
  can't see HA); a bulb whose capability is unknown (offline, attribute missing) is not
  locked.
- **Module-interface formality** — how far to formalise the module seams now vs a clean
  internal boundary with adaptive as the only module until Presence Rules lands. So far the
  seams are code-level registries the composition root fills: `register_namespace`
  (artefacts) and `register_bridge_brightness` (P2, gated since P5.2a), and the observer's
  `subscribe`, `register_guard` and `register_section` (P4).
- **Access to the HTTP API** (P3) — the API has no authentication of its own and listens on
  the container's port; it can write HA's Areas and deploy with an administrator's token.
  Cross-site requests from a browser are blocked (JSON bodies need CORS, which isn't
  enabled), and since P4 the Host allowlist refuses a DNS-rebinding page, but anyone who
  can reach the port can still drive it. Until this is settled, expose the port only to a
  trusted network (or put it behind HA ingress or a reverse proxy with authentication); the
  options left are a shared token or running as an HA add-on behind ingress.
- **Room sensors** (P4) — the observer reads a room's lux and presence sensors from its
  `adaptive_config`, because that is where the config shape puts them, so only adaptive
  rooms record lux and occupancy. They are facts about the room rather than adaptive
  settings; when a second module (Presence Rules) needs them, move them onto the room.
- **Observing beside the live system** (P4) — until cutover `ha-adaptive-light` drives the
  same bulbs. Its scheduled automations are recognised (their runs' contexts), but the late
  steps of its long maintenance transitions carry no mark, and it raises only its own
  guards, so they can be recorded as manual changes. If that history is to be kept past
  cutover, the observer could take a list of the live system's guard booleans to honour
  (the predecessor had `external_guards` for the same job); otherwise start the history
  fresh at cutover.
- **Power changes as observations** (P4) — reactive detection counts brightness changes only
  (see `docs/DESIGN.md`, "Observation"); a power change, through the plane or out of band,
  is not a reactive event. If the adaptive module needs to see a user turning a bulb off
  (the predecessor suspended maintenance on it), the observer would grow a power
  observation for it to subscribe to (P5).
- **Hard-switch source picker** (P3) — the Map offers `state` (an entity picker) and `event`
  (an event-type field) as the primary paths and keeps `device` as an advanced, manual
  entry (device id and trigger copied from an HA automation), so device ids never travel to
  the UI from the device registry. Revisit if buttons that expose only device triggers
  turn out to be common: a picker would need a device-trigger listing from HA.
- **Out-of-band power changes** — a bulb switched outside the plane (HA's UI, a vendor app,
  voice, or the live `ha-adaptive-light` during the parallel build) leaves its soft switch
  stale, so the next toggle may first "turn off" a bulb that is already off. Whether the
  plane should reflect a bulb's power back into its soft switch, and how that would avoid
  re-triggering the bridge, is open; settle it before cutover.
- **The observed lux seed** (P5.2a) — a section whose `lux_target_seed` is null learns its
  lux target from the heartbeats, from the first nightly pass on (bootstrap mode has no
  publish delay). The predecessor also read the room's lux a few times just after the
  first crossover into an unlearned section and published that as a provisional target,
  but with maintenance off, so it changed nothing in HA; reading HA's sensors from the
  runtime would also be the module scraping HA itself. Not ported; the Now tab (P5.2b)
  shows "nothing learned yet" for the first day. Revisit if that proves confusing.
- **Section display names** (deferred from P5.2b) — the scene select's options, and so the
  state every generated template reads, are the canonical section ids. Custom names (the
  predecessor's) need the templates to map a name back to its id: a generator change.
  Until then the views use the canonical names, and a section's `name` is kept but unused.
- **A light theme** (P5.2b) — the UI has one theme, dark. The adaptive views and the chart
  take every colour from the shell's CSS tokens, so a light theme would be a set of token
  values (and `color-scheme`) in `app.css`, not a change to each view.
- **Which section an observation records** (P5.2a) — the runtime reports the section it
  last fired for the room (the scene select's), falling back to the scheduled one before
  its first crossover, so a heartbeat in the seconds between a boundary and the crossover
  records the section the house was still in.
- **Crossover report after cutover** (P5.1) — the report counts only the `lm_` lane as Light
  Map's, so the live system's `al_` automations are flagged; once Light Map's own `al_`
  lane is live (P6) it is flagged alike, since the names are the same. P6 should count a
  wired module lane as Light Map's once the old system is gone.

---

## References

- `docs/DESIGN.md` — the reasoning, the three-layer model, the bridge mechanics, learning
  units, and the detailed data model.
- `ha-adaptive-light` (the predecessor) — `docs/DESIGN.md`, `docs/ROADMAP.md`, `CLAUDE.md`,
  `schema/` — the source of the proven pieces being ported.
