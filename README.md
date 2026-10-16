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

**P4: the observation core.** On top of P1's inventory (rooms from HA Areas, two-way sync),
P2's switching plane (a soft switch `input_boolean.lm_soft_<bulb>` and a soft→bulb bridge per
bulb, a hard→soft automation per hard-switch binding, deployed in their own `lm_` lane and
verified) and P3's Map (the editor and the deploy surface, with every HA write previewed
first), Light Map now watches the house: a heartbeat per room with a reading per bulb on a
fixed cadence, reactive detection of manual brightness changes (the plane's own changes are
never counted), and an event log, all archived to CSV with SQLite as a rebuildable query
layer. The **Log** tab shows the event log. A light that isn't a room bulb (an indicator
or gadget LED) can be ignored from the Map (P4.1): it gets no soft switch or bridge and
is never observed, and the Map lists it apart so it can be brought back. Deploying never
drives a bulb: each soft switch
starts at its bulb's current state. Until cutover (P6) Light Map runs beside
`ha-adaptive-light`; binding a hard switch makes its bulbs live, and the Map warns before
saving one. The adaptive module and its views arrive in P5. See
[docs/ROADMAP.md](docs/ROADMAP.md).

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
