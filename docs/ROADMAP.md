# Light Map — Roadmap

The build plan, from `docs/ARCHITECTURE.md` ("Roadmap"). That document stays the source of
truth; update both together.

Parallel build in this repo: the live `ha-adaptive-light` keeps running untouched until P6.

**Current phase: P1.** (P0 done.)

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
  the event log, storage. A platform service.
- **P5 — Adaptive module.** Port scheduler + analyser; learning units; almanac keyed by
  unit; scene + maintenance generation commanding soft switches; the Now / Analysis /
  Almanac views and the adaptive room editor.
- **P6 — Cutover.** Deploy against live HA; remove the old `al_*` system; re-express the
  house; start the almanac (fresh or re-ingested); decommission `ha-adaptive-light`. Staged,
  with the old repo kept deployable as rollback until Light Map has run clean for a few days.

## Coexistence during the parallel build

P0–P4 touch only new HA artefacts (soft switches, bridge automations, Area assignments) and
can coexist with the live adaptive system, as long as the new bridges stay unbound (no hard
switches; bridge not yet authoritative) until cutover. That coexistence boundary is the main
thing to get right during the parallel build.
