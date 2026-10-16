# CLAUDE.md — working on Light Map

**Read `docs/` before changing anything.**

- `docs/ARCHITECTURE.md` is the structural source of truth: layout, module boundaries, the
  config shape, storage, naming, the house rules, the roadmap.
- `docs/DESIGN.md` gives the reasoning and the detailed data model.
- `docs/ROADMAP.md` lists the build phases (P0–P6) and the coexistence boundary.

If a change disagrees with the docs, raise it and settle it in the docs first. Don't let the
code drift from them.

## House rules

These hold across the whole system. Several are ported from `ha-adaptive-light` and were
hard-won, so preserve them.

- **CSV is the archival source of truth; SQLite is a rebuildable query layer.** Never add
  state that only lives in SQLite; it must always be reconstructable from the CSV archive.
- **No template engine in the generators.** Generated automations embed HA Jinja verbatim;
  assemble Python data structures and let PyYAML serialise them. Rendering Jinja to produce
  Jinja makes the outer pass consume the inner one.
- **Deterministic generation.** The same config must always produce byte-identical YAML.
  Avoid dict/set ordering, timestamps, or other nondeterminism leaking into output.
- **Entity IDs only, never device IDs.** Device IDs are opaque, break on re-pairing, and
  can't be validated by an entity-existence check. A bulb's `ha_match_key` (its HA
  entity-registry entry id) is an opaque HA id the config holds as a match key for the
  snapshot reconcile only: never addressed, validated against HA, or written into generated
  artefacts. Anything that addresses a bulb uses its `entity_id`. The one carved-out
  exception: a hard switch's `device` source names a `device_id`, only to read a button
  press from a device that exposes nothing else; it appears only in that `lm_hard_`
  trigger and never addresses a light.
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
- **Observation is per bulb, long format**, pooled per learning unit at analysis time, so
  learning units can be redrawn without losing history.
- **HA Areas are the source of truth for room membership.** The config holds a committed
  snapshot (with the `ha_area_id`); registry writes are verified after the fact and
  reconcilable, never destructive by surprise.
- **The WebSocket read loop must start before subscribing.** Subscribing first deadlocks
  until timeout and leaves the event stream dead while the log still says "connected."
- **Config writes are validate-then-write**, never the reverse.
- **The HA token is never persisted in the config.** It is stripped on save/export and read
  at runtime from `LM_HA_TOKEN` or the file named by `LM_HA_TOKEN_FILE`.
- **Comments explain *why*,** especially around past bugs: leave the failure mode, not just
  the fix.

## The module interface

A module plugs into the core through four seams, so Adaptive Light is replaceable and
Presence Rules can be added without touching the trunk:

1. **Per-room config block.** A module contributes a typed section under a room (Adaptive's
   is `adaptive_config`), present only when the room enables it.
2. **Generated artefacts in its own namespace.** The module emits HA helpers/automations
   under a reserved prefix; deploy reconciles each namespace independently. The switching
   deployer never touches `al_*`, and vice versa.
3. **Observation subscription.** The module reads the platform's per-bulb heartbeats and
   reactive events; it never scrapes HA itself.
4. **Actuation vocabulary.** A module acts only by commanding soft switches (power) and
   setting bulb brightness guarded on soft-on, never by powering bulbs directly. That
   includes the bridge brightness: a module may supply the `light.turn_on` data the core
   bridge applies on soft-on for its bulbs (`core.switching.register_bridge_brightness`);
   core embeds it in the soft-on branch and owns the target and the power.

`app/core` never imports from `app/modules`. Modules are wired in by the composition root,
`app/main.py`.

## Layout and naming

- Module names and paths follow "Repository layout" in `docs/ARCHITECTURE.md` exactly.
  Later phases fill stubs in place; don't rename or move them.
- HA names: `input_boolean.lm_soft_<bulb_id>` (per bulb only; a switching group has no HA
  entity), `lm_bridge_<bulb_id>`, `lm_hard_<hard_switch_id>` for the switching plane; the
  adaptive module keeps `al_scene_`, `al_maint_`, `al_active_`, `al_hold_`,
  `sensor.al_almanac_`, `al_watchdog_`.
- Environment: `LM_HA_BASE_URL`, `LM_HA_TOKEN`, `LM_HA_TOKEN_FILE`, and `LM_CONFIG_PATH`
  (where the committed config lives; default `/data/config.json`).
- HTTP: everything under `/api/` in `app/main.py` (see "HTTP surface" in
  `docs/ARCHITECTURE.md`). An action that writes HA, or commits HA's state to the config,
  is a preview that returns a `fingerprint` and an apply that must carry it; keep it so.

## Stubs

- Every module's docstring states its responsibility and the hard rules that apply to it.
  Keep those docstrings accurate as the module is filled in.
- An unimplemented function raises `NotImplementedError`. It never passes silently or
  returns a placeholder.
- Stub signatures are provisional. The phase that fills a module may reshape its functions
  (ported modules keep their proven API), but not the module's name, path, or
  responsibility.
- Importing any module must not raise, do work, or contact Home Assistant.

## Checks

```sh
pip install -r requirements.txt
python -m unittest                 # tests against fakes and fixtures; never a real HA
                                   # (tests/test_ui.py drives the Map in headless Chromium
                                   # when Node's Playwright is installed, else skips)
python -c "import app.main"
python -c "import json, jsonschema; jsonschema.Draft202012Validator.check_schema(json.load(open('schema/config.schema.json')))"
python -c "import sqlite3; sqlite3.connect(':memory:').executescript(open('schema/storage.schema.sql').read())"
uvicorn app.main:app --port 8099   # then GET /healthz -> {"status":"ok"}
```

## Coexistence

Until cutover (P6) Light Map is built beside the live `ha-adaptive-light`. P0–P4 touch only
new HA artefacts, and the new bridges stay unbound (no hard switches; bridge not yet
authoritative). Don't cross that boundary early. Deploying must never drive a bulb: a soft
switch is set to its bulb's state before its bridge goes live, and nothing in a deploy
calls a light service.
