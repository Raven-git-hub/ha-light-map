"""Modules: behaviour layered on top of the switching plane.

A module plugs into the core through four seams, so Adaptive Light is replaceable and a
second module (Presence Rules) can be added without touching the trunk:

1. Per-room config block. A module contributes a typed section under a room (Adaptive's is
   `adaptive_config`), present only when the room enables it.
2. Generated artefacts in its own namespace. The module emits HA helpers/automations under a
   reserved prefix; deploy reconciles each namespace independently.
3. Observation subscription. The module reads the platform's per-bulb heartbeats / reactive
   events; it never scrapes HA itself.
4. Actuation vocabulary. A module acts only by commanding soft switches (power) and setting
   bulb brightness guarded on soft-on, never by powering bulbs directly. This is the
   invariant that keeps the switching plane the single source of truth for power.

Modules are wired into the core by the composition root, `app.main`.
"""
