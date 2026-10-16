"""Core: the platform trunk.

HA transport, inventory (rooms from HA Areas, bulbs, switching groups), the switching plane
(soft switches, hard->soft and soft->bulb automations), observation, config, storage,
deterministic generation, and deploy. A room never flagged for any module still gets the
full switching plane from here.

Core knows nothing about any module. Modules plug in through the seams described in
`app.modules` and are wired together in `app.main`.
"""
