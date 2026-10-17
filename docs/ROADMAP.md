# Light Map — Roadmap

The build plan, from `docs/ARCHITECTURE.md` ("Roadmap"). That document stays the source of
truth; update both together.

Parallel build in this repo: the live `ha-adaptive-light` keeps running untouched until P6.

**Current phase: P5.1** (P0–P5.0 done: the scaffold, the inventory with stable bulb
identity across HA entity renames, the switching plane with its deploy, the Map tab with its
HTTP API and deploy-with-preview, the observation core (the WebSocket observer, per-bulb
heartbeats, reactive detection, the CSV + SQLite store, the event log and the Log tab, plus a
Host-header allowlist on the API), ignored lights, and the per-room `adaptive_config` with
its editor. P5.1 is the second step of the adaptive module: the `al_` generator (helpers,
scene, maintenance and watchdog automations commanding soft switches and reading the
almanac at runtime), the bridge-brightness fragment, the almanac attribute contract P5.2
publishes to, and the `al_` deploy lane, all behind the cutover switch, which stays off
until P6: with it off, nothing generates, previews or writes `al_`, and the switching
plane's output is unchanged).

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
  - **P5.2 — Engine and views.** The scheduler port (schedule-profile internals and their
    editor, `tools/sweep.py`), the analyser, the almanac, the runtime loop, and the Now /
    Analysis / Almanac tabs.
- **P6 — Cutover.** Deploy against live HA; remove the old `al_*` system; turn the cutover
  switch on and add the `al_` lane's apply to the Map's deploy; re-express the house; start
  the almanac (fresh or re-ingested); decommission `ha-adaptive-light`. Staged, with the old
  repo kept deployable as rollback until Light Map has run clean for a few days.

## Coexistence during the parallel build

P0–P5.1 touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. That coexistence boundary is the main
thing to get right during the parallel build. Deploying the plane never drives a bulb (each
soft switch starts at its bulb's state before its bridge goes live); binding a hard switch
in the config is the step that makes a bridge authoritative. The Map warns before saving
one and marks every live bulb and group.

**The cutover switch** (P5.1): the adaptive lane's `al_` names are the live
`ha-adaptive-light`'s own, so the composition root wires the adaptive module in only when
`ADAPTIVE_ENABLED` (`app/main.py`) is on, and it stays off until P6. Off, nothing
generates, previews or writes `al_`, and the switching plane's output is byte-identical to
P5.0's. See `docs/DESIGN.md`, "The adaptive generator".
