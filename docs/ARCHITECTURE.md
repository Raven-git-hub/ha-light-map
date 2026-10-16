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
   Part of this seam is the **bridge brightness**: the soft→bulb bridge is core, but a
   module may supply, per bulb it owns, the `light.turn_on` data the bridge applies on
   soft-on (`brightness`, `brightness_pct`, `transition`; HA Jinja allowed, kept inert).
   It registers a provider with `core.switching.register_bridge_brightness`; the core
   generator embeds the fragment in that bulb's `lm_bridge_` automation, inside the
   soft-on branch, so it is guarded on soft-on by construction and can't retarget the
   bridge or touch power. Adaptive's almanac read lives in that fragment (P5), not in core.
   A bulb no module claims takes the plain path (its last brightness).

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
                     policy. Ported from ha-adaptive-light.
      analyser.py    Nightly learning pipeline. Pools per-bulb observations per LEARNING
                     UNIT into one target. Ported; behavioural changes gated behind flags.
      almanac.py     The learned model, keyed by learning unit; pushed to HA over REST.
      generate.py    Scene + maintenance + watchdog automations. Commands SOFT SWITCHES for
                     power; reads learning-unit targets for brightness. conditions: [] on
                     scene automations.
      runtime.py     The adaptive loop: subscribe to core observation, run the scheduler,
                     fire crossovers, push the almanac.
static/              Vanilla JS and CSS, no build step; desktop-oriented, dark.
  index.html         Tab shell: Map, Now, Analysis, Almanac, Config, Log.
  app.css            The theme and the components every view shares.
  app.js             The shell: tab router, API helper, DOM/notice/dialog helpers.
  map.css, map.js    The Map tab (P3). Each later view adds its own pair.
  log.css, log.js    The Log tab (P4): the event log, read-only.
schema/
  config.schema.json The config contract (see "Top-level config shape").
  storage.schema.sql SQLite DDL (see "Storage model").
tools/
  doctor.py          Probe a real HA instance; validate connectivity and entities; print the
                     rooms the inventory would build. Read-only.
  sweep.py           A year of section-boundary computations, checking for unexpected
                     collapses.
tests/               Unit tests (stdlib unittest, `python -m unittest`) against fakes and
                     fixtures; never a real HA. `test_ui.py` also drives the Map and the Log
                     in headless Chromium (`tests/ui/map_ui.cjs`, `log_ui.cjs`, Node's
                     Playwright) where that is installed, and is skipped where it isn't.
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

- `version` — schema version (2 since P2); the container migrates forward on load.
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
  - `bulbs[]`: `{ id (slug), name, entity_id (light.*), ha_match_key?, hard_switches[] }` —
    a soft-switch helper and a soft→bulb bridge are implicit (generated) per bulb.
    `ha_match_key` is the light's HA entity-registry entry id: an opaque key the reconcile
    matches on, so a bulb survives its entity_id being renamed in HA (see
    `docs/DESIGN.md`, "Bulb identity").
  - `switching_groups[]`: `{ id (slug), name, members (bulb ids), hard_switches[] }` — a
    named set of member bulbs plus its hard-switch bindings. A group mints **no** soft
    switch of its own: a hard switch bound to it acts on every member's per-bulb soft
    switch.
  - `adaptive_config` — the per-module block, **required when `adaptive` is true** and
    forbidden when false: `{ lux_sensors[], presence_sensors[], schedule_profile,
    learning_units[], scenes }`, where `learning_units[]` = `{ id (slug), name,
    members (bulb ids) }` and `scenes` is the per-section matrix keyed by learning-unit id ×
    section.

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
- `schedule_profiles[]` — reusable time-section schedules (adaptive module). A profile with
  id `default` must always exist.
- `learning` — adaptive analyser tuning.

**Id scopes** follow the HA names the ids end up in. Room ids are global
(`sensor.al_almanac_<room>`), and no two rooms share an HA Area. Bulb ids are global, because
each names a soft switch and a bridge (`input_boolean.lm_soft_<id>`, `lm_bridge_<id>`) —
including the bulbs in `unassigned[]`. Switching-group ids share that one namespace: a
group names no HA entity, but a hard switch binds to a bulb or a group, and keeping the two
apart lets either be named by id alone. Hard-switch ids are global (`lm_hard_<id>`).
Learning-unit ids are unique within their room. Schedule-profile ids are global. A bulb's
`ha_match_key`, when present, is unique across all bulbs. The schema checks shape;
`app/core/config.py` cross-checks these scopes and every reference.

The cross-check also refuses a hard switch wired into a feedback loop: a source in Light
Map's own `lm_` namespace, a `toggle` whose source is a bulb Light Map drives, or an event
source on an HA core event (`state_changed`, `call_service`, `automation_reloaded`, ...),
which fires for the plane's own work. Deploy adds the one check that needs HA's state: no
`toggle` on an HA group of bulbs Light Map drives.

As of P4 the core sections (`homeassistant`, `system`, `rooms[]`, `unassigned[]`) are
validated in depth; `learning`, `schedule_profiles` and `adaptive_config` fill in with the
adaptive module (P5).

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
  `deferred_ms` (how long the tick waited for a guard). Unique on (`room_id`, `ts`).
- `bulb_reading` — long format, one row per bulb per heartbeat: `id`, `heartbeat_id` (fk),
  `bulb_id`, `is_on` (nullable: HA had no usable state for the bulb), `brightness`
  (nullable; 0–255).
- `reactive_event` — one consolidated manual change in a room (one reactive window):
  `id`, `ts` (the window's start), `room_id`, `section`, `window_seconds`, `lux_before`,
  `lux_after`, `occupancy`. Unique on (`room_id`, `ts`).
- `reactive_change` — long format, one row per bulb of the room for each reactive event:
  `id`, `reactive_event_id` (fk), `bulb_id`, `is_on_before`, `is_on_after`,
  `brightness_before`, `brightness_after` (not `before`/`after`: SQL keywords), `changed`
  (the user changed this bulb by more than the min-delta). There is no stored weight:
  weighting is the analyser's (P5), applied at analysis time, so retuning it reweights
  history.
- `almanac` — insert-only learned snapshots: `id`, `ts`, `room_id`, `section`,
  `learning_unit_id`, `lux_target`, `lux_margin`, `maintenance_enabled`, `max_step_pct`,
  `trust_weight`, `valid_from`, `mode`.
- `event_log` — `id`, `ts`, `level` (`debug` | `info` | `warning` | `error`), `category`,
  `room_id` (nullable), `message`, `detail` (json).

Each table has a CSV stream under `<data_dir>/archive/<table>/<YYYY-MM>.csv` (by the row's
UTC month) with the table's columns minus the surrogate ids; a child row carries its
parent's natural key (`room_id`, `ts`) instead of its id, so the archive needs nothing from
SQLite to be read back. Every write goes to the CSV first. The query layer
(`<data_dir>/light_map.db`) records how much of each archive file it holds, and is rebuilt
from the archive at startup when that doesn't match (deleted, behind after a failed write,
or broken). Event-log retention prunes the query layer only; the archive keeps everything.

---

## Naming and namespaces

- **Product / container:** Light Map. Repo `ha-light-map`.
- **Switching plane:** `input_boolean.lm_soft_<bulb_id>`, one per bulb and only per bulb;
  automations `lm_bridge_<bulb_id>` (soft→bulb) and `lm_hard_<hard_switch_id>` (hard→soft).
  A switching group has no HA entity: an HA group of `input_boolean`s can only be made in
  `configuration.yaml`, which Light Map never edits, and with no group entity there is no
  group state to keep in sync. A hard switch bound to a group acts on every member's soft
  switch. (A single group-state entity for dashboards, if ever wanted, would be an HA light
  group of the member bulbs — a future enhancement, not a soft switch.) The soft switch's
  helper is named `LM Soft <bulb_id>`, because HA derives a network-created helper's id from
  its slugified name.
- **Adaptive module:** keeps the recognizable `al_` names (`al_scene_`, `al_maint_`,
  `al_active_`, `al_hold_`, `sensor.al_almanac_`, `al_watchdog_`), so the ported generator
  barely changes.
- Deploy reconciles `lm_*`/`lm_soft_*` and `al_*` as **separate lanes** — the switching
  deployer never touches `al_*`, and vice versa. A prefix is reserved by registering its
  generator (`core.generate.register_namespace`); a name belongs to the longest registered
  prefix it starts with, so even an adaptive lane folded under `lm_al_` would stay its own.

---

## HTTP surface (P3, P4)

`app/main.py` serves the UI's API under `/api/` (JSON; an error is `{error, detail}`,
with `problems` when there are several) beside `/healthz` and the static UI. The HA client
is made in the app's lifespan from the environment; without a token the Map still serves
the committed config and every HA action says why it can't run. The lifespan also opens
the store and starts the observer (P4), which opens the WebSocket once the config has a
room with bulbs to observe.

| | |
| --- | --- |
| Reads | `GET /api/status`, `/api/map`, `/api/config` (export, token-free), `/api/entities` (hard-switch source picker), `/api/log` (the event log, filtered by minimum level, category and room; P4) |
| Config edits | `PUT /api/rooms/{room}/adaptive`, `PUT /api/bulbs/{bulb}/hard_switches`, `POST /api/rooms/{room}/groups`, `PUT`/`DELETE /api/rooms/{room}/groups/{group}` |
| Area edits (HA first) | `POST /api/rooms`, `PUT /api/rooms/{room}/name`, `POST /api/bulbs/{bulb}/move/preview` then `/move` |
| Previewed actions | `GET /api/sync/preview` then `POST /api/sync`; `GET /api/deploy/preview` then `POST /api/deploy`; `GET /api/areas/preview` then `POST /api/areas/deploy` |

- **Preview, then apply what was shown.** A preview writes nothing and returns a
  `fingerprint` of what it showed; the apply must carry it, recomputes from fresh state, and
  is refused (409 `preview_stale`) if the result digests differently. So a deploy, a sync,
  an Area write or a move is never applied unless it was previewed as it now stands.
- **Validate-then-write**, every edit; a move also checks its config before its HA write.
  A config on disk that doesn't load is never overwritten from the Map.
- **Edits carry what they were made against.** Each edit of an existing bulb's hard
  switches, group, or room's adaptive flag sends the `version` the Map showed as `base`, and
  is refused (409 `edit_stale`) if it has changed since, so a stale tab never silently
  reverts another edit, and an acknowledgement covers only what was shown.
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
thing to get right during the parallel build. Deploying the plane never drives a bulb: each
soft switch is set to its bulb's current on/off before its bridge goes live, and a bridge
fires only on its soft switch changing between `off` and `on`. Binding a hard switch in
the config is the step that makes a bridge authoritative, so leave `hard_switches` empty
(or bind only switches the live system doesn't use) until cutover. The Map warns before
saving one (the API refuses it unacknowledged), marks every live bulb and group, and the
deploy preview lists the hard switches it puts live and the automations (the live
system's `al_` ones among them) that already drive the same bulbs.

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
  internal boundary with adaptive as the only module until Presence Rules lands. So far the
  seams are code-level registries the composition root fills: `register_namespace`
  (artefacts) and `register_bridge_brightness` (P2), and the observer's `subscribe`,
  `register_guard` and `register_section` (P4).
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

---

## References

- `docs/DESIGN.md` — the reasoning, the three-layer model, the bridge mechanics, learning
  units, and the detailed data model.
- `ha-adaptive-light` (the predecessor) — `docs/DESIGN.md`, `docs/ROADMAP.md`, `CLAUDE.md`,
  `schema/` — the source of the proven pieces being ported.
