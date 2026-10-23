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

**P6a: the last code before the cutover.** On top of P1's inventory (rooms from HA Areas,
two-way sync), P2's switching plane (a soft switch `input_boolean.lm_soft_<bulb>` and a
soft→bulb bridge per bulb, a hard→soft automation per hard-switch binding, deployed in their
own `lm_` lane and verified), P3's Map (the editor and the deploy surface, with every HA
write previewed first) and P4's observation core (heartbeats per room with a reading per
bulb, reactive detection of manual brightness changes, an event log in the **Log** tab, all
archived to CSV with SQLite as a rebuildable query layer; P4.1 lets a light that isn't a
room bulb be ignored), the Adaptive Light module is built. A room flagged adaptive on the
Map opens a dedicated editor (the **Config** tab): its lux and presence sensors, its
schedule profile, which of its bulbs are adaptive and which stay switched-only (a bulb Home
Assistant reports as on/off only can't be made adaptive), how the adaptive bulbs pool into
learning units, and the section × unit scene matrix. P5.1 generates the adaptive
automations (in their own `al_` lane), P5.2a the engine that drives them (the section
scheduler, the nightly learner pooling each learning unit's bulbs, the almanac it publishes
to Home Assistant, the runtime loop), and P5.2b the read-only **Now**, **Analysis** and
**Almanac** tabs and the schedule-profile editor. P6a makes the scheduler sound across
clock changes and lets the Map's deploy apply the `al_` lane after `lm_`. All of the
adaptive module stays switched off by default (it is on only when the deployment sets
`LM_ADAPTIVE_ENABLED=1`, at the cutover, P6b), because its `al_` names are the live
`ha-adaptive-light`'s own. Deploying never drives a bulb: each soft switch starts at its
bulb's current state.
Until cutover (P6b) Light Map runs beside `ha-adaptive-light`; binding a hard switch makes
its bulbs live, and the Map warns before saving one. See [docs/ROADMAP.md](docs/ROADMAP.md).

To see what Light Map makes of your Home Assistant (read-only):

```sh
export LM_HA_BASE_URL=http://homeassistant.local:8123 LM_HA_TOKEN=...
python tools/doctor.py                      # add --config path/to/config.json to compare
```

Two offline tools for the adaptive engine (they never contact Home Assistant):

```sh
python tools/sweep.py [--config path/to/config.json]      # a year of section boundaries
python tools/compare_analyser.py --config path/to/config.json --tz Europe/London  # learner, legacy vs current
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

Observations, the event log and the query database live in the config's directory (on the
volume in the container), or `system.data_dir` if the config sets one.

Light Map answers only requests addressed to `localhost` or `127.0.0.1` unless you list the
names you reach it by in `LM_ALLOWED_HOSTS` (comma-separated, e.g.
`LM_ALLOWED_HOSTS=lightmap.lan,192.168.1.20`); anything else gets a 400. That stops a
DNS-rebinding web page from driving the API through your browser. Behind a reverse proxy,
list the name the proxy sends upstream (often the container's name, e.g. `light-map`),
unless it passes the original Host on. IPv6 literals can't be listed: browse by name or
IPv4 address.

The API has no authentication of its own and can write to Home Assistant with your token:
expose port 8099 only to a trusted network (see "Open decisions" in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)). The Host allowlist is not authentication.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): structure, module boundaries, config shape,
  storage, naming, house rules.
- [docs/DESIGN.md](docs/DESIGN.md): the reasoning and the detailed data model.
- [docs/ROADMAP.md](docs/ROADMAP.md): the build phases.
- [CLAUDE.md](CLAUDE.md): contributor guidance.

## License

MIT. See [LICENSE](LICENSE).
