# Light Map — Roadmap

The build plan, from `docs/ARCHITECTURE.md` ("Roadmap"). That document stays the source of
truth; update both together.

Parallel build in this repo: the live `ha-adaptive-light` keeps running untouched until P6.

**Current phase: P6a** (P0–P5.2b done: the scaffold, the inventory with stable bulb
identity across HA entity renames, the switching plane with its deploy, the Map tab with its
HTTP API and deploy-with-preview, the observation core (the WebSocket observer, per-bulb
heartbeats, reactive detection, the CSV + SQLite store, the event log and the Log tab, plus a
Host-header allowlist on the API), ignored lights, the per-room `adaptive_config` with its
editor, the `al_` generator with its deploy lane and the almanac attribute contract, the
adaptive engine (the schedule profiles and the ported scheduler, the analyser pooling
per-bulb readings per learning unit, the almanac's store and publish, the runtime loop, the
bridge's gated fallback), and the adaptive views (the read-only Now / Analysis / Almanac
tabs and the schedule-profile editor), all behind the cutover switch, which stays off until
P6b. P6a is the last code before the cutover: the scheduler's clock changes (a section whose
start the clocks skip on a spring-forward day starts at the gap's end, so none is reported
as run yet never in force; `tools/sweep.py` checks it, at clock-changing sites too), the
crossover report counting Light Map's own `al_` automations as its own, and the Map's deploy
applying the module lanes after `lm_` under one fingerprint. All of it is pure computation
or gated on the cutover switch: with it off, the live app generates, previews, applies and
writes exactly what P5.2b did. P6b, the cutover itself, done by hand, comes next).

## Phases

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
    sections' display names are deferred (they need a generator change).
- **P6 — Cutover.** In two steps:
  - **P6a — Final pre-cutover code.** The scheduler's clock changes (a section whose start
    the clocks skip is moved to the gap's end, never reported as run yet never in force,
    and `tools/sweep.py` checks it at clock-changing sites), the crossover report counting
    Light Map's own `al_` automations as its own, and the Map's deploy applying the module
    lanes after `lm_` under one fingerprint. All of it pure computation or behind the
    cutover switch, which stays off: the live app's output is P5.2b's byte for byte.
  - **P6b — Cutover** — ✅ done for the Living Room (2026-10-22; the old system's only room).
    By hand against the real HA: removed the old `al_*` system; set `LM_ADAPTIVE_ENABLED=1`
    on the deployment; deployed (`lm_`, then `al_`); re-expressed the house; started the
    almanac fresh. The four-button Zigbee remote was bound the next day. The old
    `ha-adaptive-light` is kept deployable as rollback until Light Map has run clean for a
    few days, then decommissioned.

## Coexistence during the parallel build

P0–P6a touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. That coexistence boundary is the main
thing to get right during the parallel build. Deploying the plane never drives a bulb (each
soft switch starts at its bulb's state before its bridge goes live); binding a hard switch
in the config is the step that makes a bridge authoritative. The Map warns before saving
one and marks every live bulb and group.

**The cutover switch** (P5.1): the adaptive lane's `al_` names are the live
`ha-adaptive-light`'s own, so the composition root wires the adaptive module in only when
`ADAPTIVE_ENABLED` (`app/main.py`) is on, which reads `LM_ADAPTIVE_ENABLED=1` from the
environment and is off by default. Off, nothing
generates, previews or writes `al_`, no adaptive runtime runs (nothing fires a scene or
publishes an almanac), the switching plane's output is byte-identical to P5.0's, and the
adaptive views read no live `al_` entity (P5.2b). See `docs/DESIGN.md`, "The adaptive generator".
