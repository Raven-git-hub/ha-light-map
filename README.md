# Light Map

Light Map is a self-hosted container that runs a home's lighting through Home Assistant as
two planes:

- **The switching plane.** Every bulb gets a soft switch (an `input_boolean`), the single
  source of truth for its power. Hard switches bind to the soft switch, and a soft→bulb
  bridge makes the bulb follow it.
- **Modules.** Behaviour layered on top, acting only by commanding soft switches and setting
  brightness. **Adaptive Light**, which learns a preferred light level for each section of
  the day and drives brightness, is the first module.

Rooms come from Home Assistant Areas and are edited two-way from the Map. A room that never
enables a module still gets the full switching plane.

Light Map is a ground-up rebuild of `ha-adaptive-light`.

## Status

**P3: the Map.** On top of P1's inventory (rooms from HA Areas, two-way sync) and P2's
switching plane (a soft switch `input_boolean.lm_soft_<bulb>` and a soft→bulb bridge per
bulb, a hard→soft automation per hard-switch binding, deployed in their own `lm_` lane and
verified), the Map tab is the editor: rooms and bulbs, the adaptive flag, hard switches
and switching groups, moving bulbs and creating or renaming rooms (written to HA's Areas),
sync from HA with every change shown before it is saved, and deploy with a mandatory
preview (the plan, a read-only report of what else in HA drives the same bulbs, and the
coexistence note). Deploying never drives a bulb: each soft switch starts at its bulb's
current state. Until cutover (P6) Light Map runs beside `ha-adaptive-light`; binding a hard
switch makes its bulbs live, and the Map warns before saving one. The adaptive module and
its views arrive in P5. See [docs/ROADMAP.md](docs/ROADMAP.md).

To see what Light Map makes of your Home Assistant (read-only):

```sh
export LM_HA_BASE_URL=http://homeassistant.local:8123 LM_HA_TOKEN=...
python tools/doctor.py                      # add --config path/to/config.json to compare
```

## Run

```sh
cp .env.example .env     # set LM_HA_BASE_URL and LM_HA_TOKEN
docker compose up --build
curl http://localhost:8099/healthz
```

The Map is at <http://localhost:8099/>. The config is kept at `LM_CONFIG_PATH` (default
`/data/config.json`, on the container's volume); on a first run there is none yet: use
**Sync from HA** to build the rooms from your Areas, review, and save. To run outside the
container, point `LM_CONFIG_PATH` somewhere writable:

```sh
LM_CONFIG_PATH=data/config.json uvicorn app.main:app --port 8099
```

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): structure, module boundaries, config shape,
  storage, naming, house rules.
- [docs/DESIGN.md](docs/DESIGN.md): the reasoning and the detailed data model.
- [docs/ROADMAP.md](docs/ROADMAP.md): the build phases.
- [CLAUDE.md](CLAUDE.md): contributor guidance.

## License

MIT. See [LICENSE](LICENSE).
