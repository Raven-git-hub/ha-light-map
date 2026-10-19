# Light Map — Roadmap

The build plan, from `docs/ARCHITECTURE.md` ("Roadmap"). That document stays the source of
truth; update both together.

Parallel build in this repo: the live `ha-adaptive-light` keeps running untouched until P6.

**Current phase: P5.2b** (P0–P5.2a done: the scaffold, the inventory with stable bulb
identity across HA entity renames, the switching plane with its deploy, the Map tab with its
HTTP API and deploy-with-preview, the observation core (the WebSocket observer, per-bulb
heartbeats, reactive detection, the CSV + SQLite store, the event log and the Log tab, plus a
Host-header allowlist on the API), ignored lights, the per-room `adaptive_config` with its
editor, the `al_` generator with its deploy lane and the almanac attribute contract, and the
adaptive engine: the schedule profiles (config version 5) and the ported scheduler, the
analyser pooling per-bulb readings per learning unit, the almanac's store and publish, the
runtime loop, and the bridge's gated fallback, all behind the cutover switch, which stays
off until P6. P5.2b is the adaptive views: the read-only Now / Analysis / Almanac tabs and
the schedule-profile editor, with uPlot vendored for the Analysis chart. They read the
config, HA's location and Light Map's own store; a live `al_` entity, which the old system
owns until P6, only with the cutover switch on. No engine or generator change; section
display names are deferred. P6, the cutover, comes next).

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
- **P6 — Cutover.** Deploy against live HA; remove the old `al_*` system; turn the cutover
  switch on and add the `al_` lane's apply to the Map's deploy; re-express the house; start
  the almanac (fresh or re-ingested); decommission `ha-adaptive-light`. Staged, with the old
  repo kept deployable as rollback until Light Map has run clean for a few days.

## Coexistence during the parallel build

P0–P5.2 touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. That coexistence boundary is the main
thing to get right during the parallel build. Deploying the plane never drives a bulb (each
soft switch starts at its bulb's state before its bridge goes live); binding a hard switch
in the config is the step that makes a bridge authoritative. The Map warns before saving
one and marks every live bulb and group.

**The cutover switch** (P5.1): the adaptive lane's `al_` names are the live
`ha-adaptive-light`'s own, so the composition root wires the adaptive module in only when
`ADAPTIVE_ENABLED` (`app/main.py`) is on, and it stays off until P6. Off, nothing
generates, previews or writes `al_`, no adaptive runtime runs (nothing fires a scene or
publishes an almanac), the switching plane's output is byte-identical to P5.0's, and the
adaptive views read no live `al_` entity (P5.2b). See `docs/DESIGN.md`, "The adaptive generator".
