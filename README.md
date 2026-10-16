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

**P1: inventory and rooms-from-Areas.** The HA transport (REST + WebSocket + Area/entity
registry), the config loader and validator, and rooms built from HA Areas with two-way sync.
It reads Home Assistant and writes only Area assignments; nothing is generated or deployed
yet. See [docs/ROADMAP.md](docs/ROADMAP.md).

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

The UI shell is at <http://localhost:8099/>.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): structure, module boundaries, config shape,
  storage, naming, house rules.
- [docs/DESIGN.md](docs/DESIGN.md): the reasoning and the detailed data model.
- [docs/ROADMAP.md](docs/ROADMAP.md): the build phases.
- [CLAUDE.md](CLAUDE.md): contributor guidance.

## License

MIT. See [LICENSE](LICENSE).
