// The Config tab (P5.0): the adaptive rooms, and the per-room adaptive editor.
//
// #config lists the rooms flagged adaptive (the flag is the Map's checkbox), each opening
// its editor; #config/<room> is the editor, which the Map's "Adaptive config" button opens
// too. There is no global adaptive config: each room's is its own.
//
// The editor works on a draft of the room's adaptive_config and saves it whole (PUT
// /api/rooms/<room>/adaptive_config) against the room's version as it was shown, like every
// other edit: a save from a stale editor is refused (edit_stale), and the editor reloads the
// room as it now is rather than overwrite what changed. Its edits keep the draft in the
// shape the server checks (the learning units partition exactly the adaptive bulbs; the
// scene matrix has a cell per unit per section), and it adds the one rule the server can't
// check, since the config can't see Home Assistant: a bulb HA reports as on/off only can't
// be adaptive. A bulb whose capability is unknown (offline) is never locked.
//
// Read from and written through /api (see app/main.py); every name is set as text, never
// as markup. Nothing here writes HA: the adaptive automations arrive in P5.1.

"use strict";

(() => {
  const { h, fill, api, notify, fail, confirmBox } = LM;

  const SECTION_NAMES = {
    sunrise: "Sunrise",
    day: "Day",
    afternoon: "Afternoon",
    sunset: "Sunset",
    night: "Night",
    sleep: "Sleep",
  };
  const ENTITY_ID = /^[a-z0-9_]+\.[a-z0-9_]+$/;
  // As the server's cross-check: a lux sensor reports a number; a presence sensor must
  // read on/off; neither may be one of Light Map's own entities.
  const SENSORS = {
    lux_sensors: {
      label: "Lux sensors",
      domains: ["sensor"],
      hint: "Illuminance sensors (sensor.*) in the room. Several are averaged; one reading 0 (its floor) is left out.",
      placeholder: "sensor.living_room_lux",
    },
    presence_sensors: {
      label: "Presence sensors",
      domains: ["binary_sensor", "input_boolean"],
      hint: "Entities that read on/off (binary_sensor.* or input_boolean.*): the room counts as occupied while any is on.",
      placeholder: "binary_sensor.living_room_motion",
    },
  };
  const OWN_PREFIXES = ["lm_", "al_"];
  const NEW_UNIT = "";

  // The section knobs: per section, room-wide. `scale` shows a fraction as a percentage.
  const KNOBS = [
    {
      key: "maintenance_enabled",
      label: "Maintain",
      check: true,
      title:
        "Run lux maintenance in this section (off by default for Night and Sleep, where lux sits at the sensor's floor)",
    },
    {
      key: "lux_target_seed",
      label: "Lux seed",
      min: 0,
      step: "any",
      nullable: true,
      placeholder: "observe",
      title: "A seed for the section's lux target; empty to observe it at the first crossover",
    },
    { key: "lux_margin", label: "Margin (lx)", min: 0, step: 0.5, title: "The deadband either side of the target" },
    {
      key: "max_step_pct",
      label: "Max step %",
      min: 0,
      exclusiveMin: true,
      max: 100,
      step: 0.5,
      scale: 100,
      title: "The largest single maintenance nudge, as a percentage of the current brightness",
    },
    { key: "transition_seconds", label: "Fade (s)", min: 0, step: 1, integer: true, title: "The crossover fade" },
  ];

  LM.view("config", async (main, roomId) => {
    if (roomId) await editor(main, roomId);
    else await roomList(main);
  });

  // -- The adaptive rooms --------------------------------------------------------------

  async function roomList(main) {
    const host = h("section", { class: "adaptive-rooms" }, h("p", { class: "muted" }, "Loading…"));
    main.append(host);
    const map = await api("GET", "/api/map");
    const rooms = map.rooms.filter((r) => r.adaptive);
    fill(
      host,
      h("h2", {}, "Adaptive rooms"),
      h(
        "p",
        { class: "muted" },
        "Each adaptive room has its own config: its sensors, its schedule profile, which of its bulbs learn a brightness and how they pool, and what each section of the day does. A room is flagged adaptive with its checkbox on the Map.",
      ),
      rooms.length
        ? h(
            "ul",
            { class: "room-links" },
            rooms.map((room) =>
              h(
                "li",
                { dataset: { room: room.id } },
                h("a", { class: "room-link", href: `#config/${encodeURIComponent(room.id)}` }, room.name),
                h("span", { class: "muted" }, ` · ${summary(room.adaptive_config)}`),
              ),
            ),
          )
        : h(
            "div",
            { class: "box no-adaptive-rooms" },
            "No room is adaptive yet. Tick ",
            h("strong", {}, "Adaptive"),
            " beside a room on the ",
            h("a", { href: "#map" }, "Map"),
            ", then open its config here or with its Adaptive config button.",
          ),
    );
  }

  function summary(adaptiveConfig) {
    const bulbs = adaptiveConfig?.adaptive_bulbs?.length || 0;
    const units = adaptiveConfig?.learning_units?.length || 0;
    if (!bulbs) return "no adaptive bulbs yet";
    return `${plural(bulbs, "adaptive bulb")} in ${plural(units, "learning unit")}`;
  }

  // -- The editor ----------------------------------------------------------------------

  // The room being edited: `view` as the server sent it, `draft` the adaptive_config being
  // edited, `baseline` the saved block's key (what Save compares with), `opened` the
  // draft's key as the editor opened it (what the user's own edits are measured from:
  // it differs from `baseline` only by the lock's demotions), `bad` the fields holding
  // something invalid ({message, raw}: what was typed, shown again when the field is
  // redrawn), `demoted` the bulbs the lock took out of the adaptive tier on opening,
  // `serverProblems` what the last refused save said.
  let ed = null;

  async function editor(main, roomId) {
    main.append(
      h(
        "section",
        { id: "adaptive-editor", class: "adaptive-editor", dataset: { room: roomId } },
        nav(),
        h("p", { class: "muted" }, "Loading the room’s adaptive config…"),
      ),
    );
    // Only the user's own edits ask before leaving: the lock's demotions are the editor's,
    // and come back on every opening. A save in flight lands whether or not the view stays.
    LM.protect(
      () => Boolean(ed && ed.roomId === roomId && edited() && !ed.saving),
      "This room’s adaptive config changes",
    );
    await load(roomId);
  }

  function nav() {
    return h(
      "nav",
      { class: "editor-nav" },
      h("a", { class: "back-to-map", href: "#map" }, "← Back to the Map"),
      h("a", { class: "all-rooms", href: "#config" }, "All adaptive rooms"),
    );
  }

  function host(roomId) {
    const el = document.getElementById("adaptive-editor");
    return el && el.dataset.room === roomId ? el : null;
  }

  async function load(roomId) {
    if (!host(roomId)) return;
    ed = null;
    let view;
    try {
      view = await api("GET", `/api/rooms/${encodeURIComponent(roomId)}/adaptive_config`);
    } catch (err) {
      const el = host(roomId);
      if (el) fill(el, nav(), h("div", { class: "box error editor-unavailable" }, err.message));
      if (err.error !== "not_adaptive" && err.error !== "not_found") fail(err);
      return;
    }
    if (!host(roomId)) return; // another view, or another room, is showing now
    ed = {
      roomId,
      view,
      draft: structuredClone(view.adaptive_config),
      baseline: key(view.adaptive_config),
      opened: null,
      bad: {},
      demoted: new Set(),
      serverProblems: [],
      saving: false,
    };
    // The lock applies to what is saved too: a bulb that can't dim but is adaptive (hand
    // edited, or saved before HA reported it) is shown switched-only in the draft, with a
    // note, and saving makes it so. Unknown capability never demotes anything.
    for (const bulb of view.bulbs) {
      if (bulb.capability.locked && ed.draft.adaptive_bulbs.includes(bulb.id)) {
        ed.demoted.add(bulb.id);
        setTier(bulb.id, false);
      }
    }
    ed.opened = key(ed.draft);
    render();
  }

  // -- The draft -----------------------------------------------------------------------

  // A key that compares two configs by meaning, not by the order lists happen to be in.
  function key(config) {
    const sorted = (list) => [...list].sort();
    return canon({
      ...config,
      lux_sensors: sorted(config.lux_sensors),
      presence_sensors: sorted(config.presence_sensors),
      adaptive_bulbs: sorted(config.adaptive_bulbs),
      learning_units: config.learning_units
        .map((u) => ({ ...u, members: sorted(u.members) }))
        .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)),
    });
  }

  function canon(value) {
    if (Array.isArray(value)) return `[${value.map(canon).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value)
        .sort()
        .map((k) => `${JSON.stringify(k)}:${canon(value[k])}`)
        .join(",")}}`;
    }
    return JSON.stringify(value ?? null);
  }

  // dirty: what Save would change (the user's edits and the lock's demotions); edited:
  // the user's own edits, which Revert drops and leaving asks about.
  const dirty = () => key(ed.draft) !== ed.baseline;
  const edited = () => key(ed.draft) !== ed.opened;
  const bulbOf = (id) => ed.view.bulbs.find((b) => b.id === id);
  const unitOf = (id) => ed.draft.learning_units.find((u) => u.members.includes(id));
  const isAdaptive = (id) => ed.draft.adaptive_bulbs.includes(id);
  const nameOf = (id) => bulbOf(id)?.name || id;

  // In the room's bulb order, so what is saved reads the way the Map does.
  function inRoomOrder(ids) {
    const wanted = new Set(ids);
    return ed.view.bulbs.map((b) => b.id).filter((id) => wanted.has(id));
  }

  function slug(text) {
    return (
      String(text)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "") || "unit"
    );
  }

  // Unit ids are unique within the room and key the almanac; a bulb's own unit takes the
  // bulb's id, a switching group's the group's, where free.
  function newUnitId(base) {
    const taken = new Set(ed.draft.learning_units.map((u) => u.id));
    const id = slug(base);
    if (!taken.has(id)) return id;
    let n = 2;
    while (taken.has(`${id}_${n}`)) n += 1;
    return `${id}_${n}`;
  }

  // Out of its unit; a unit left with no members goes.
  function takeOut(id) {
    for (const unit of ed.draft.learning_units) unit.members = unit.members.filter((m) => m !== id);
    ed.draft.learning_units = ed.draft.learning_units.filter((u) => u.members.length);
  }

  function ownUnit(id) {
    ed.draft.learning_units.push({ id: newUnitId(id), name: nameOf(id), members: [id] });
  }

  // Adaptive: in its own unit (the default). Switched-only: out of its unit and the tier.
  function setTier(id, adaptive) {
    if (adaptive === isAdaptive(id)) return;
    takeOut(id);
    if (adaptive) {
      ed.draft.adaptive_bulbs = inRoomOrder([...ed.draft.adaptive_bulbs, id]);
      ownUnit(id);
    } else {
      ed.draft.adaptive_bulbs = ed.draft.adaptive_bulbs.filter((b) => b !== id);
    }
    reshape();
  }

  function moveTo(id, unitId) {
    takeOut(id);
    const unit = ed.draft.learning_units.find((u) => u.id === unitId);
    if (unit) unit.members = inRoomOrder([...unit.members, id]);
    else ownUnit(id);
    reshape();
  }

  // The group's members that can learn: active bulbs of the room HA doesn't report as
  // on/off only.
  function eligible(group) {
    return inRoomOrder(group.members.filter((id) => bulbOf(id) && !bulbOf(id).capability.locked));
  }

  function learnedAsOne(group) {
    const members = eligible(group);
    const unit = members.length ? unitOf(members[0]) : null;
    return Boolean(unit) && unit.members.length === members.length && members.every((id) => unit.members.includes(id));
  }

  // One click for the aligned case: the group's members that can learn become adaptive and
  // pool into one unit, named after the group.
  function learnGroup(group) {
    const members = eligible(group);
    for (const id of members) takeOut(id);
    ed.draft.adaptive_bulbs = inRoomOrder([...ed.draft.adaptive_bulbs, ...members]);
    ed.draft.learning_units.push({ id: newUnitId(group.id), name: group.name, members });
    reshape();
  }

  function splitUnit(unit) {
    ed.draft.learning_units = ed.draft.learning_units.filter((u) => u !== unit);
    for (const id of unit.members) ownUnit(id);
    reshape();
  }

  // The matrix follows the units: a cell (auto) for each new unit, none for a gone one,
  // as the server's normalisation does.
  function reshape() {
    const ids = ed.draft.learning_units.map((u) => u.id);
    for (const section of ed.view.sections) {
      const row = ed.draft.scenes[section];
      const cells = {};
      for (const id of ids) cells[id] = row.units[id] || { mode: "auto" };
      row.units = cells;
    }
  }

  function sensorProblem(field, entityId) {
    const spec = SENSORS[field];
    if (!ENTITY_ID.test(entityId)) return `${entityId || "That"} is not an entity id (e.g. ${spec.placeholder}).`;
    const [domain, objectId] = entityId.split(".");
    if (OWN_PREFIXES.some((p) => objectId.startsWith(p))) return `${entityId} is one of Light Map’s own entities.`;
    if (!spec.domains.includes(domain)) {
      return field === "presence_sensors"
        ? `${entityId} doesn’t read on/off: use a binary_sensor or input_boolean (a person or device tracker reports home, which reads as unoccupied).`
        : `${entityId} is not a sensor.* (an illuminance sensor).`;
    }
    if (ed.draft[field].includes(entityId)) return `${entityId} is already listed.`;
    return null;
  }

  function problems() {
    return [...Object.values(ed.bad).map((b) => b.message), ...ed.serverProblems];
  }

  // -- Rendering -----------------------------------------------------------------------

  function render() {
    const el = ed && host(ed.roomId);
    if (!el) return;
    const { view } = ed;
    // A unit's name error goes with the unit: no field would be left to fix it in.
    const units = new Set(ed.draft.learning_units.map((u) => u.id));
    for (const field of Object.keys(ed.bad)) {
      if (field.startsWith("unit:") && !units.has(field.slice(5))) delete ed.bad[field];
    }
    fill(
      el,
      nav(),
      h(
        "header",
        { class: "editor-head" },
        h("h2", {}, `Adaptive config · ${view.room.name}`),
        h("span", { class: "muted area-id" }, `Area ${view.room.ha_area_id}`),
      ),
      bar(),
      h(
        "p",
        { class: "muted intro" },
        "Which of this room’s bulbs learn a brightness, how they pool into learning units, and what each section of the day does. Saving changes the config only: nothing reaches Home Assistant until the adaptive automations arrive (P5.1). A switched-only bulb keeps the plain switching plane: it comes on at its last brightness.",
      ),
      view.ha.available
        ? null
        : h(
            "div",
            { class: "box warn ha-unavailable" },
            `${view.ha.error}: whether each bulb can dim is unknown, so none is locked.`,
          ),
      card("sensors", "Sensors", sensorsSection()),
      card("schedule", "Schedule profile", profileSection()),
      card("bulbs", "Bulbs", bulbsSection()),
      card("units", "Learning units", unitsSection()),
      card("scenes", "Scenes", scenesSection()),
    );
    refreshBar();
  }

  function card(name, title, ...body) {
    return h("section", { class: `editor-card ${name}-card` }, h("h3", {}, title), ...body);
  }

  function bar() {
    return h(
      "div",
      { class: "editor-bar" },
      h("span", { id: "editor-state", class: "pill" }),
      h("span", { class: "spacer" }),
      h("button", { id: "editor-revert", onclick: () => revert().catch(fail) }, "Revert"),
      h("button", { id: "editor-save", class: "primary", onclick: () => save().catch(fail) }, "Save"),
      h("div", { id: "editor-problems", role: "alert" }),
    );
  }

  // The save bar and the problems list: after every edit, without re-rendering the rest (a
  // knob being typed in keeps its focus).
  function refreshBar() {
    const state = document.getElementById("editor-state");
    if (!state || !ed) return;
    const pending = dirty();
    const own = edited();
    const found = problems();
    const demoted = [...ed.demoted].filter((id) => !isAdaptive(id)).length;
    state.textContent = ed.saving
      ? "Saving…"
      : own
        ? "Unsaved changes"
        : pending
          ? `Not saved yet: ${plural(demoted, "bulb")} that can’t dim to make switched-only`
          : "Saved";
    state.className = pending ? "pill warn" : "pill ok";
    document.getElementById("editor-save").disabled = ed.saving || !pending || found.length > 0;
    document.getElementById("editor-revert").disabled = ed.saving || !own;
    fill(
      document.getElementById("editor-problems"),
      found.length
        ? h(
            "div",
            { class: "box error" },
            h("p", {}, "Not saved yet:"),
            h(
              "ul",
              { class: "plain" },
              found.map((p) => h("li", {}, p)),
            ),
          )
        : null,
    );
  }

  // After any edit: the server's last refusal no longer describes the draft.
  function changed({ full = false } = {}) {
    ed.serverProblems = [];
    if (full) render();
    else refreshBar();
  }

  function sensorsSection() {
    return h(
      "div",
      { class: "sensors" },
      Object.entries(SENSORS).map(([field, spec]) => {
        const listId = `lm-${field.replace("_", "-")}`;
        const input = h("input", {
          type: "text",
          class: "sensor-input",
          list: listId,
          placeholder: spec.placeholder,
          autocomplete: "off",
          "aria-label": `Add a ${spec.label.toLowerCase().replace(/s$/, "")}`,
        });
        const error = h("p", { class: "form-error", role: "alert", hidden: true });
        const add = () => {
          const entityId = input.value.trim();
          const problem = sensorProblem(field, entityId);
          if (problem) {
            error.textContent = problem;
            error.hidden = false;
            return;
          }
          ed.draft[field] = [...ed.draft[field], entityId];
          changed({ full: true });
        };
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") add();
        });
        const named = Object.fromEntries((ed.view.suggestions[field] || []).map((s) => [s.entity_id, s.name]));
        return h(
          "div",
          { class: "sensor-list", dataset: { field } },
          h("h4", {}, spec.label),
          h("p", { class: "muted small" }, spec.hint),
          ed.draft[field].length
            ? h(
                "ul",
                { class: "chips" },
                ed.draft[field].map((entityId) =>
                  h(
                    "li",
                    { class: "chip", dataset: { sensor: entityId } },
                    h("code", {}, entityId),
                    named[entityId] ? h("span", { class: "muted" }, ` ${named[entityId]}`) : null,
                    h(
                      "button",
                      {
                        class: "icon remove-sensor",
                        "aria-label": `Remove ${entityId}`,
                        onclick: () => {
                          ed.draft[field] = ed.draft[field].filter((s) => s !== entityId);
                          changed({ full: true });
                        },
                      },
                      "×",
                    ),
                  ),
                ),
              )
            : h("p", { class: "muted none" }, "None."),
          h(
            "div",
            { class: "sensor-add" },
            input,
            h("button", { class: "add-sensor", onclick: add }, "Add"),
            h(
              "datalist",
              { id: listId },
              (ed.view.suggestions[field] || []).map((s) => h("option", { value: s.entity_id }, s.name)),
            ),
          ),
          error,
        );
      }),
    );
  }

  function profileSection() {
    const profiles = ed.view.schedule_profiles;
    const current = ed.draft.schedule_profile;
    const choices = profiles.some((p) => p.id === current) ? profiles : [...profiles, { id: current, name: current }];
    const select = h(
      "select",
      {
        id: "profile-select",
        onchange: (event) => {
          ed.draft.schedule_profile = event.target.value;
          changed();
        },
      },
      choices.map((p) => h("option", { value: p.id }, p.name === p.id ? p.id : `${p.name} (${p.id})`)),
    );
    select.value = current;
    return h(
      "div",
      { class: "profile" },
      h("label", { class: "field" }, h("span", {}, "The section times this room follows"), select),
      h(
        "p",
        { class: "muted small" },
        "Profiles are shared between rooms. When each section starts and ends (sun-relative or fixed) is edited with the scheduler, which arrives with the adaptive engine (P5.2).",
      ),
    );
  }

  function bulbsSection() {
    const { view } = ed;
    if (!view.bulbs.length) {
      return h(
        "p",
        { class: "muted none" },
        "This room has no bulbs to make adaptive (an ignored light is never offered).",
      );
    }
    return [
      h(
        "p",
        { class: "muted small" },
        "Adaptive bulbs learn a brightness per section and follow the scenes; switched-only bulbs keep the plain switching plane. A bulb made adaptive starts in a learning unit of its own.",
      ),
      h(
        "table",
        { class: "bulb-tiers" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            ["Bulb", "Tier", "Learning unit", "Notes"].map((title) => h("th", { scope: "col" }, title)),
          ),
        ),
        h("tbody", {}, view.bulbs.map(bulbRow)),
      ),
      groupsBlock(),
    ];
  }

  function bulbRow(bulb) {
    const adaptive = isAdaptive(bulb.id);
    const locked = bulb.capability.locked;
    const unit = unitOf(bulb.id);
    const tierButton = (value, label) =>
      h(
        "button",
        {
          class: `tier-${value}${(value === "adaptive") === adaptive ? " on" : ""}`,
          "aria-pressed": String((value === "adaptive") === adaptive),
          disabled: value === "adaptive" && locked,
          title: value === "adaptive" && locked ? bulb.capability.note : null,
          onclick: () => {
            setTier(bulb.id, value === "adaptive");
            changed({ full: true });
          },
        },
        label,
      );
    const notes = [];
    if (bulb.capability.note) {
      notes.push(
        h(
          "span",
          { class: `capability ${locked ? "locked" : bulb.capability.dimmable === null ? "unknown" : ""}` },
          locked ? h("span", { class: "badge lock" }, "LOCKED") : null,
          locked ? " " : null,
          bulb.capability.note,
        ),
      );
    }
    if (ed.demoted.has(bulb.id)) {
      notes.push(
        h(
          "span",
          { class: "demoted" },
          "It was adaptive; saving makes it switched-only, since it can’t follow a learned brightness.",
        ),
      );
    }
    if (bulb.groups.length) {
      const names = bulb.groups.map((id) => ed.view.groups.find((g) => g.id === id)?.name || id);
      notes.push(h("span", { class: "muted" }, `Switching group: ${names.join(", ")}`));
    }
    return h(
      "tr",
      { class: `bulb-tier${adaptive ? " adaptive" : ""}${locked ? " locked" : ""}`, dataset: { bulb: bulb.id } },
      h(
        "td",
        { class: "bulb-cell", "data-label": "Bulb" },
        h("span", { class: "bulb-name" }, bulb.name),
        h("code", { class: "entity" }, bulb.entity_id),
      ),
      h(
        "td",
        { class: "tier-cell", "data-label": "Tier" },
        h(
          "div",
          { class: "segmented", role: "group", "aria-label": `${bulb.name}: tier` },
          tierButton("adaptive", "Adaptive"),
          tierButton("switched", "Switched-only"),
        ),
      ),
      h(
        "td",
        { class: "unit-cell", "data-label": "Learning unit" },
        adaptive && unit ? unitSelect(bulb, unit) : h("span", { class: "muted" }, "—"),
      ),
      h("td", { class: "notes-cell", "data-label": "Notes" }, notes.length ? notes : null),
    );
  }

  function unitSelect(bulb, unit) {
    const select = h(
      "select",
      {
        class: "unit-select",
        "aria-label": `${bulb.name}: learning unit`,
        onchange: (event) => {
          moveTo(bulb.id, event.target.value);
          changed({ full: true });
        },
      },
      ed.draft.learning_units.map((u) => h("option", { value: u.id }, u.name)),
      unit.members.length > 1 ? h("option", { value: NEW_UNIT }, "A unit of its own") : null,
    );
    select.value = unit.id;
    return select;
  }

  function groupsBlock() {
    const groups = ed.view.groups;
    if (!groups.length) return null;
    return h(
      "div",
      { class: "learn-groups" },
      h("h4", {}, "Learn a switching group as one unit"),
      h(
        "p",
        { class: "muted small" },
        "For bulbs that are really one light (a chandelier, a strip in sections): makes the group’s bulbs adaptive and pools them into one learning unit. Units may also cross switching groups: pick a bulb’s unit above.",
      ),
      h(
        "div",
        { class: "group-buttons" },
        groups.map((group) => {
          const members = eligible(group);
          const locked = group.members.filter((id) => bulbOf(id)?.capability.locked);
          const done = learnedAsOne(group);
          return h(
            "div",
            { class: "learn-group-row", dataset: { group: group.id } },
            h(
              "button",
              {
                class: "learn-group",
                dataset: { group: group.id },
                disabled: !members.length || done,
                onclick: () => {
                  learnGroup(group);
                  changed({ full: true });
                },
              },
              `Learn ${group.name} as one unit`,
            ),
            h(
              "span",
              { class: "muted small" },
              done
                ? " Already one learning unit."
                : !members.length
                  ? " None of its bulbs can dim."
                  : ` ${members.map(nameOf).join(", ")}`,
              locked.length && !done && members.length
                ? `; ${locked.map(nameOf).join(", ")} can’t dim and stays switched-only.`
                : "",
            ),
          );
        }),
      ),
    );
  }

  function unitsSection() {
    const units = ed.draft.learning_units;
    if (!units.length) {
      return h(
        "p",
        { class: "muted none" },
        "No learning units yet: make a bulb adaptive and it gets a unit of its own.",
      );
    }
    return [
      h(
        "p",
        { class: "muted small" },
        "Every adaptive bulb is in exactly one unit; a unit’s bulbs share one learned target per section, and each keeps its own soft switch. Rename a unit freely: its id, which its learning is kept under, stays.",
      ),
      h(
        "div",
        { class: "units" },
        units.map((unit) => {
          const name = h("input", {
            type: "text",
            class: "unit-name-input",
            value: ed.bad[`unit:${unit.id}`] ? ed.bad[`unit:${unit.id}`].raw : unit.name,
            "aria-label": `Name of learning unit ${unit.id}`,
            oninput: (event) => {
              const text = event.target.value.trim();
              if (text) {
                unit.name = text;
                delete ed.bad[`unit:${unit.id}`];
              } else {
                ed.bad[`unit:${unit.id}`] = {
                  message: `Learning unit ${unit.id} needs a name.`,
                  raw: event.target.value,
                };
              }
              changed();
            },
            // The new name shows in the bulbs' unit pickers and the matrix's header.
            onchange: () => {
              if (!ed.bad[`unit:${unit.id}`]) render();
            },
          });
          return h(
            "div",
            { class: "unit-card", dataset: { unit: unit.id } },
            h("div", { class: "unit-head" }, name, h("code", { class: "muted unit-id" }, unit.id)),
            h("p", { class: "unit-members" }, unit.members.map(nameOf).join(", ")),
            unit.members.length > 1
              ? h(
                  "button",
                  {
                    class: "link split-unit",
                    onclick: () => {
                      splitUnit(unit);
                      changed({ full: true });
                    },
                  },
                  "Split: each bulb its own unit",
                )
              : null,
          );
        }),
      ),
    ];
  }

  function scenesSection() {
    const units = ed.draft.learning_units;
    const table = h(
      "table",
      { class: "scene-matrix" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { scope: "col", class: "section-head" }, "Section"),
          units.map((u) => h("th", { scope: "col", class: "unit-head", dataset: { unit: u.id } }, u.name)),
          KNOBS.map((k) => h("th", { scope: "col", class: "knob-head", title: k.title }, k.label)),
        ),
      ),
      h(
        "tbody",
        {},
        ed.view.sections.map((section) => sceneRow(section, units)),
      ),
    );
    return [
      h(
        "p",
        { class: "muted small" },
        "Auto learns from what you do; Off holds the unit off for that section (the learner never decides this for you). The tuning knobs are per section, for the whole room. Maintenance is off by default for Night and Sleep, where lux sits at the sensor’s noise floor.",
      ),
      units.length ? null : h("p", { class: "muted none" }, "No learning units yet, so no Auto/Off columns."),
      h("div", { class: "matrix-wrap" }, table),
    ];
  }

  function sceneRow(section, units) {
    const row = ed.draft.scenes[section];
    return h(
      "tr",
      { dataset: { section } },
      h("th", { scope: "row", class: "section-name" }, SECTION_NAMES[section] || section),
      units.map((unit) => modeCell(section, row, unit)),
      KNOBS.map((spec) => h("td", { class: "knob-cell", dataset: { knob: spec.key } }, knobInput(section, row, spec))),
    );
  }

  function modeCell(section, row, unit) {
    const cell = row.units[unit.id];
    const buttons = ["auto", "off"].map((mode) =>
      h(
        "button",
        {
          class: `mode-${mode}${cell.mode === mode ? " on" : ""}`,
          "aria-pressed": String(cell.mode === mode),
          onclick: () => {
            cell.mode = mode;
            for (const b of buttons) {
              const on = b.classList.contains(`mode-${mode}`);
              b.classList.toggle("on", on);
              b.setAttribute("aria-pressed", String(on));
            }
            changed();
          },
        },
        mode === "auto" ? "Auto" : "Off",
      ),
    );
    return h(
      "td",
      { class: "mode-cell", dataset: { unit: unit.id } },
      h("div", { class: "segmented", role: "group", "aria-label": `${SECTION_NAMES[section]}: ${unit.name}` }, buttons),
    );
  }

  function knobInput(section, row, spec) {
    const field = `${section}:${spec.key}`;
    const label = `${SECTION_NAMES[section]}: ${spec.label}`;
    if (spec.check) {
      return h("input", {
        type: "checkbox",
        class: `knob knob-${spec.key}`,
        checked: row[spec.key] === true,
        title: spec.title,
        "aria-label": label,
        onchange: (event) => {
          row[spec.key] = event.target.checked;
          changed();
        },
      });
    }
    // A field holding something invalid is redrawn with what was typed, beside its error,
    // so the error can always be fixed where it is shown.
    const saved =
      row[spec.key] === null ? "" : spec.scale ? Number((row[spec.key] * spec.scale).toFixed(4)) : row[spec.key];
    const shown = ed.bad[field] ? ed.bad[field].raw : saved;
    const input = h("input", {
      type: "number",
      class: `knob knob-${spec.key}`,
      value: String(shown),
      min: spec.min,
      max: spec.max ?? null,
      step: spec.step,
      placeholder: spec.placeholder || null,
      title: spec.title,
      "aria-label": label,
      oninput: (event) => {
        const problem = setKnob(row, spec, event.target.value);
        if (problem) ed.bad[field] = { message: `${label}: ${problem}`, raw: event.target.value };
        else delete ed.bad[field];
        event.target.classList.toggle("invalid", Boolean(problem));
        changed();
      },
    });
    if (ed.bad[field]) input.classList.add("invalid");
    return input;
  }

  // Parse what was typed into a knob; set it and return null, or return what is wrong (the
  // draft keeps its last good value, and Save waits).
  function setKnob(row, spec, text) {
    const raw = text.trim();
    if (!raw) {
      if (spec.nullable) {
        row[spec.key] = null;
        return null;
      }
      return "needs a value";
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) return "not a number";
    if (spec.integer && !Number.isInteger(n)) return "a whole number of seconds";
    if (spec.exclusiveMin ? n <= spec.min : n < spec.min)
      return `must be more than ${spec.exclusiveMin ? "" : "or equal to "}${spec.min}`;
    if (spec.max !== undefined && n > spec.max) return `must be at most ${spec.max}`;
    // Checked as it would be saved: a percentage too small to survive the rounding would
    // save as 0, which the server refuses.
    const value = spec.scale ? Number((n / spec.scale).toFixed(6)) : n;
    if (spec.exclusiveMin && value <= spec.min) return "too small to save";
    row[spec.key] = value;
    return null;
  }

  // -- Saving --------------------------------------------------------------------------

  async function save() {
    if (!ed || ed.saving || !dirty() || problems().length) return;
    const { roomId, view } = ed;
    ed.saving = true;
    refreshBar();
    let result;
    try {
      result = await api("PUT", `/api/rooms/${encodeURIComponent(roomId)}/adaptive_config`, {
        adaptive_config: ed.draft,
        base: view.version,
      });
    } catch (err) {
      if (ed?.roomId === roomId) ed.saving = false;
      if (err.error === "edit_stale" || err.error === "not_adaptive") {
        // Changed since the editor showed it (another tab, a sync). Expected, so not
        // fail(): say so, and show the room as it is now; saving this draft would undo it.
        notify(err.message, { kind: "error" });
        await load(roomId);
        return;
      }
      if (ed?.roomId === roomId && err.problems.length) {
        ed.serverProblems = err.problems;
        refreshBar();
        return;
      }
      refreshBar();
      throw err;
    }
    const changes = result.changes.length;
    notify(
      `Saved ${view.room.name}’s adaptive config${changes ? ` (${plural(changes, "change")})` : ""}. Nothing is deployed: the adaptive automations arrive in P5.1.`,
      { kind: "ok", timeout: 8000 },
    );
    await load(roomId);
  }

  async function revert() {
    if (!ed || !edited()) return;
    const sure = await confirmBox(
      "Revert?",
      ["Drop the unsaved changes and show the room’s adaptive config as saved."],
      {
        confirm: "Revert",
        danger: true,
      },
    );
    if (sure) await load(ed.roomId);
  }

  function plural(n, singular, pluralForm = `${singular}s`) {
    return `${n} ${n === 1 ? singular : pluralForm}`;
  }
})();
