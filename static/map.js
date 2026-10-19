// The Map tab (P3): the inventory-and-switching editor and the deploy surface.
//
// Rooms (HA Areas) run down the page, then the Unassigned bucket, then a collapsed
// "Ignored (N)" section (P4.1) listing the lights set aside, each with Un-ignore. Per room:
// its name with the ADAPTIVE checkbox, its bulbs that aren't ignored (name, entity_id, Move
// and Ignore, and the generated soft switch, which opens the hard-switch popup), its
// switching groups (each opens the group popup; ADD makes one), and the Adaptive Config
// button, which opens the room's adaptive editor (the Config tab, #config/<room>; P5.0)
// with a line saying how many of its bulbs are adaptive; an adaptive bulb's row says so.
// An ignored light is in no row and no picker: the server leaves it out of every room's
// bulbs.
//
// Everything is read from and written through /api (see app/main.py). The rules the
// server enforces are mirrored here so the user sees them before saving, never instead of
// the server: every HA-touching action shows its preview first and applies it with the
// preview's fingerprint; binding a hard switch shows the coexistence warning and needs
// an explicit acknowledgement; nothing is ever applied that wasn't shown.

"use strict";

(() => {
  const { h, fill, api, notify, fail, dialog, footer, confirmBox, list } = LM;

  // ignoredOpen: whether the Ignored section is unfolded, kept across re-renders.
  const state = { map: null, entities: null, entitiesError: null, ignoredOpen: false };

  const MODES = {
    toggle: "Each press: if any of its bulbs is on, they all turn off; otherwise they all turn on.",
    press: "Each press sets its bulbs to one state, on or off (e.g. an “all off” button).",
    follow: "Its bulbs track the switch’s own on/off: a latching switch or relay (entity sources only).",
  };

  // Hints for the event-type field; any event type can be typed.
  const EVENT_TYPES = [
    "zha_event",
    "deconz_event",
    "hue_event",
    "zwave_js_value_notification",
    "zwave_js_notification",
    "shelly.click",
    "lutron_caseta_button_event",
    "homematic.keypress",
  ];

  const ENTITY_ID = /^[a-z0-9_]+\.[a-z0-9_]+$/;
  const DEVICE_ID = /^[A-Za-z0-9_-]+$/;
  const RESERVED_TRIGGER_KEYS = ["trigger", "platform", "device_id", "id"];

  let formSerial = 0;

  // -- Loading -------------------------------------------------------------------------

  LM.view("map", async (main) => {
    main.append(
      toolbar(),
      h("div", { id: "map-banner" }),
      h("div", { id: "map-table" }, h("p", { class: "muted" }, "Loading the map…")),
    );
    await reload();
  });

  async function reload() {
    if (!document.getElementById("map-table")) return; // another tab is showing
    state.map = await api("GET", "/api/map");
    render();
    refreshSync();
  }

  function toolbar() {
    return h(
      "div",
      { class: "map-toolbar" },
      h("button", { id: "sync-button", onclick: () => syncDialog().catch(fail) }, "Sync from HA"),
      h("span", { id: "sync-state", class: "pill" }, "Checking HA…"),
      h("button", { id: "new-room", onclick: () => roomNameDialog(null).catch(fail) }, "+ New room"),
      h("span", { class: "spacer" }),
      h("span", { id: "live-summary", class: "muted" }),
      h(
        "button",
        {
          id: "areas-button",
          title: "Write the committed snapshot back to Home Assistant's Areas (previewed first)",
          onclick: () => areasDialog().catch(fail),
        },
        "Write Areas to HA…",
      ),
      h("button", { id: "deploy-button", class: "primary", onclick: () => deployDialog().catch(fail) }, "Deploy…"),
    );
  }

  // -- The table -----------------------------------------------------------------------

  function render() {
    const host = document.getElementById("map-table");
    if (!host) return;
    const map = state.map;
    const liveCount = map.live.bulbs.length;
    document.getElementById("live-summary").textContent =
      `${plural(map.live.hard_switches, "hard switch", "hard switches")} · ${plural(liveCount, "live bulb")}`;
    const newRoom = document.getElementById("new-room");
    // Before the first reviewed sync there is no config to add a room to (the API refuses).
    newRoom.disabled = !map.config.exists;
    newRoom.title = map.config.exists ? "Creates an Area in Home Assistant" : "Sync from HA first";
    const banner = document.getElementById("map-banner");
    banner.replaceChildren();
    if (!map.config.exists) {
      banner.append(
        h(
          "div",
          { class: "box first-run" },
          "No config has been saved yet. Use ",
          h("strong", {}, "Sync from HA"),
          " to build the rooms from Home Assistant’s Areas, review them, and save.",
        ),
      );
    }
    if (liveCount) {
      banner.append(
        h(
          "div",
          { class: "box warn coexistence-banner" },
          h("span", { class: "badge live" }, "LIVE"),
          ` ${plural(liveCount, "bulb is", "bulbs are")} bound to hard switches. Once deployed, pressing one drives its bulbs through Light Map, beside ha-adaptive-light, until cutover (P6).`,
        ),
      );
    }
    fill(
      host,
      h(
        "table",
        { class: "map-table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", { scope: "col" }, "Room · bulb"),
            h("th", { scope: "col" }, "Soft switch"),
            h("th", { scope: "col" }, "Switching groups"),
            h("th", { scope: "col" }, "Adaptive config"),
          ),
        ),
        map.rooms.map(roomBody),
        unassignedBody(map.unassigned),
      ),
      ignoredSection(map.ignored),
    );
  }

  // Said where the bulbs would be, so a room that seems empty isn't a mystery.
  function ignoredNote(count) {
    return count ? ` ${plural(count, "ignored light")} (listed under Ignored, below).` : "";
  }

  function roomBody(room) {
    const span = Math.max(1, room.bulbs.length) + 1;
    const body = h("tbody", { class: "room", dataset: { room: room.id } });
    body.append(
      h(
        "tr",
        { class: "room-row" },
        h(
          "th",
          { scope: "rowgroup", colspan: 2, class: "room-cell" },
          h("span", { class: "room-name" }, room.name),
          h(
            "button",
            {
              class: "icon rename",
              title: "Rename the room (renames its HA Area)",
              "aria-label": `Rename ${room.name}`,
              onclick: () => roomNameDialog(room).catch(fail),
            },
            "✎",
          ),
          h(
            "label",
            { class: "adaptive-toggle", title: "Flag this room for the Adaptive Light module" },
            h("input", {
              type: "checkbox",
              class: "adaptive-flag",
              checked: room.adaptive,
              onchange: (event) => toggleAdaptive(room, event.target).catch(fail),
            }),
            " Adaptive",
          ),
          h("span", { class: "muted area-id" }, `Area ${room.ha_area_id}`),
        ),
        h("td", { class: "groups-cell", rowspan: span }, groupsCell(room)),
        h(
          "td",
          { class: "adaptive-cell", rowspan: span },
          h(
            "button",
            {
              class: "adaptive-config",
              disabled: !room.adaptive,
              title: room.adaptive ? "Open this room’s adaptive editor" : "Tick Adaptive first",
              onclick: () => {
                location.hash = `#config/${encodeURIComponent(room.id)}`;
              },
            },
            "Adaptive config",
          ),
          room.adaptive ? h("div", { class: "muted adaptive-summary" }, adaptiveSummary(room)) : null,
        ),
      ),
    );
    if (!room.bulbs.length) {
      body.append(
        h(
          "tr",
          { class: "bulb-row empty" },
          h("td", { colspan: 2, class: "muted" }, `No bulbs in this room.${ignoredNote(room.ignored_count)}`),
        ),
      );
    }
    for (const bulb of room.bulbs) body.append(bulbRow(bulb, room));
    return body;
  }

  function adaptiveSummary(room) {
    const bulbs = room.adaptive_config?.adaptive_bulbs?.length || 0;
    const units = room.adaptive_config?.learning_units?.length || 0;
    if (!bulbs) return "No adaptive bulbs yet: every bulb is switched-only.";
    return `${plural(bulbs, "adaptive bulb")} in ${plural(units, "learning unit")}`;
  }

  // An adaptive bulb of an adaptive room (adaptive-ness lives in the room's adaptive
  // config, not on the bulb).
  function isAdaptiveBulb(bulb, room) {
    return Boolean(room?.adaptive && room.adaptive_config?.adaptive_bulbs?.includes(bulb.id));
  }

  function unassignedBody(bucket) {
    const span = Math.max(1, bucket.bulbs.length) + 1;
    const body = h("tbody", { class: "room unassigned", dataset: { room: "" } });
    body.append(
      h(
        "tr",
        { class: "room-row" },
        h(
          "th",
          { scope: "rowgroup", colspan: 2, class: "room-cell" },
          h("span", { class: "room-name" }, bucket.name),
          h(
            "span",
            { class: "muted area-id" },
            "Bulbs with no HA Area: no switching groups or modules until moved into a room.",
          ),
        ),
        h("td", { class: "groups-cell muted", rowspan: span }, "—"),
        h("td", { class: "adaptive-cell muted", rowspan: span }, "—"),
      ),
    );
    if (!bucket.bulbs.length) {
      body.append(
        h(
          "tr",
          { class: "bulb-row empty" },
          h("td", { colspan: 2, class: "muted" }, `Every bulb is in a room.${ignoredNote(bucket.ignored_count)}`),
        ),
      );
    }
    for (const bulb of bucket.bulbs) body.append(bulbRow(bulb, null));
    return body;
  }

  function bulbRow(bulb, room) {
    const direct = bulb.hard_switches.length;
    const viaGroups = bulb.live_via.filter((via) => via.owner.kind === "group");
    return h(
      "tr",
      { class: bulb.live ? "bulb-row live" : "bulb-row", dataset: { bulb: bulb.id } },
      h(
        "td",
        { class: "bulb-cell" },
        h("span", { class: "bulb-name" }, bulb.name),
        isAdaptiveBulb(bulb, room)
          ? h("span", { class: "tier-tag", title: "Adaptive: learns its brightness (see Adaptive config)" }, "adaptive")
          : null,
        h("code", { class: "entity" }, bulb.entity_id),
        h(
          "button",
          {
            class: "link move",
            title: "Move to another room (writes its HA Area)",
            onclick: () => moveDialog(bulb, room).catch(fail),
          },
          "Move…",
        ),
        h(
          "button",
          {
            class: "link ignore",
            title: "Not a room bulb (an indicator or gadget LED)? Set it aside: no soft switch or bridge",
            "aria-label": `Ignore ${bulb.name}`,
            onclick: (event) => ignoreBulb(bulb, event.currentTarget).catch(fail),
          },
          "Ignore",
        ),
      ),
      h(
        "td",
        { class: "soft-cell" },
        h(
          "button",
          {
            class: "soft",
            title: "Bind hard switches to this soft switch",
            onclick: () => softPopup(bulb).catch(fail),
          },
          bulb.soft,
        ),
        bulb.live ? h("span", { class: "badge live", title: liveTitle(bulb) }, "LIVE") : null,
        h(
          "span",
          { class: "muted switch-count" },
          direct ? plural(direct, "hard switch", "hard switches") : "no hard switch",
          viaGroups.length ? ` · via ${[...new Set(viaGroups.map((v) => v.owner.name))].join(", ")}` : "",
        ),
      ),
    );
  }

  function groupsCell(room) {
    const nameOf = (id) => room.bulbs.find((b) => b.id === id)?.name || id;
    return h(
      "div",
      { class: "groups" },
      room.groups.map((group) =>
        h(
          "button",
          {
            class: group.live ? "group live" : "group",
            dataset: { group: group.id },
            title: "Edit this switching group",
            onclick: () => groupPopup(room, group).catch(fail),
          },
          h(
            "span",
            { class: "group-name" },
            group.name,
            group.live ? h("span", { class: "badge live" }, "LIVE") : null,
          ),
          h("span", { class: "group-members" }, group.members.map(nameOf).join(", ")),
          h(
            "span",
            { class: "muted" },
            group.hard_switches.length
              ? plural(group.hard_switches.length, "hard switch", "hard switches")
              : "no hard switch",
          ),
        ),
      ),
      h(
        "button",
        {
          class: "add-group",
          disabled: !room.bulbs.length,
          title: room.bulbs.length ? "Group bulbs a hard switch toggles together" : "The room has no bulbs",
          onclick: () => groupPopup(room, null).catch(fail),
        },
        "+ Add group",
      ),
    );
  }

  // -- Ignored lights ------------------------------------------------------------------

  function ignoredSection(ignored) {
    const section = h(
      "details",
      { class: "ignored-section", open: state.ignoredOpen },
      h("summary", {}, h("span", { class: "ignored-title" }, `Ignored (${ignored.length})`)),
      h(
        "p",
        { class: "muted" },
        "Lights that aren’t room bulbs (indicator LEDs, gadget LEDs), set aside: no soft switch or bridge, never observed, never moved. Home Assistant still has them, so a sync keeps following them, and they can be brought back.",
      ),
      ignored.length
        ? h(
            "table",
            { class: "ignored-table" },
            h(
              "tbody",
              {},
              ignored.map((entry) =>
                h(
                  "tr",
                  { class: "ignored-row", dataset: { ignored: entry.id } },
                  h("td", {}, h("span", { class: "bulb-name" }, entry.name), h("code", { class: "entity" }, entry.entity_id)),
                  h("td", { class: "muted ignored-room" }, entry.room_name),
                  h(
                    "td",
                    { class: "actions" },
                    h(
                      "button",
                      {
                        class: "link unignore",
                        title: "Bring it back: a soft switch and bridge on the next deploy",
                        "aria-label": `Un-ignore ${entry.name}`,
                        onclick: (event) => unignoreBulb(entry, event.currentTarget).catch(fail),
                      },
                      "Un-ignore",
                    ),
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "muted none" }, "No light is ignored. Use Ignore on a bulb that isn’t a room bulb."),
    );
    section.addEventListener("toggle", () => {
      state.ignoredOpen = section.open;
    });
    return section;
  }

  async function ignoreBulb(bulb, button) {
    // Nothing to lose: ignore at once, so setting a dozen indicator LEDs aside is a dozen
    // clicks. Anything it would drop is listed first, and the server insists on that too.
    const drops = bulb.ignore_drops;
    if (drops.length) {
      const sure = await confirmBox(
        `Ignore ${bulb.name}?`,
        [
          "It gets no soft switch or bridge. Ignoring it also drops:",
          list(drops),
          "The light itself is untouched; it can be un-ignored, but these don’t come back with it.",
        ],
        { confirm: "Ignore it", danger: true },
      );
      if (!sure) return;
    }
    if (await setIgnored(bulb, true, drops.length > 0, button)) {
      notify(
        `Ignored ${bulb.name}. Deploy to remove its soft switch and bridge from Home Assistant (the light itself is left as it is).`,
        { kind: "ok", timeout: 8000 },
      );
    }
  }

  async function unignoreBulb(entry, button) {
    if (await setIgnored(entry, false, false, button)) {
      notify(`${entry.name} is back in ${entry.room_name}. Deploy to give it a soft switch and bridge.`, {
        kind: "ok",
        timeout: 8000,
      });
    }
  }

  // True once saved. `base` is the bulb's ignore_version, which covers what ignoring would
  // drop: a confirmation given for the list on screen can't drop something added since.
  // The button stays disabled while the request runs, so a double click sends one request
  // (the second, made against the version the first replaced, would read as stale).
  async function setIgnored(bulb, ignored, confirmDrops, button) {
    button.disabled = true;
    try {
      await api("PUT", `/api/bulbs/${bulb.id}/ignored`, {
        ignored,
        confirm_drops: confirmDrops,
        base: bulb.ignore_version,
      });
    } catch (err) {
      button.disabled = false;
      if (err.error === "no_change") {
        await reload(); // already so (another tab did it): nothing went wrong
        return false;
      }
      if (err.error === "edit_stale") {
        // Changed since the Map showed it (another tab, a sync). Expected, so not fail():
        // nothing to log. Show the Map as it is now.
        notify(err.message, { kind: "error" });
        await reload();
        return false;
      }
      throw err;
    }
    await reload();
    return true;
  }

  function liveTitle(bulb) {
    const by = bulb.live_via.map((via) =>
      via.owner.kind === "group" ? `${via.name} (group ${via.owner.name})` : via.name,
    );
    return `Live: driven by ${by.join(", ")}`;
  }

  // -- Hard switches -------------------------------------------------------------------

  async function ensureEntities() {
    try {
      state.entities = (await api("GET", "/api/entities")).entities;
      state.entitiesError = null;
    } catch (err) {
      state.entitiesError = err.message;
    }
    let entities = document.getElementById("lm-entities");
    if (!entities) entities = document.body.appendChild(h("datalist", { id: "lm-entities" }));
    fill(entities, ...(state.entities || []).map((e) => h("option", { value: e.entity_id }, `${e.name} (${e.state})`)));
    if (!document.getElementById("lm-event-types")) {
      document.body.append(
        h(
          "datalist",
          { id: "lm-event-types" },
          EVENT_TYPES.map((t) => h("option", { value: t })),
        ),
      );
    }
  }

  // Ignored lights count, as on the server: a toggle on one would loop once it was un-ignored.
  function mappedBulb(entityId) {
    const all = [...state.map.rooms.flatMap((r) => r.bulbs), ...state.map.unassigned.bulbs, ...state.map.ignored];
    return all.some((b) => b.entity_id === entityId);
  }

  // JSON with sorted keys, so two bindings that do the same thing compare equal.
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

  const acts = (b) => canon([b.source, b.mode, b.turn ?? null]);

  function sourceText(source) {
    if (source.type === "state") {
      const known = (state.entities || []).find((e) => e.entity_id === source.entity_id);
      return `entity · ${source.entity_id}${known ? ` (${known.name})` : ""}`;
    }
    if (source.type === "event") {
      return `event · ${source.event_type}${source.event_data ? ` ${JSON.stringify(source.event_data)}` : ""}`;
    }
    const t = source.trigger || {};
    return `device · ${source.device_id} · ${[t.domain, t.type, t.subtype].filter(Boolean).join(" ")}`;
  }

  function modeText(binding) {
    return binding.mode === "press" ? `press → ${binding.turn}` : binding.mode;
  }

  // A staged list of bindings for one bulb or group. Nothing is saved from here: the
  // popup that owns it saves the whole list at once.
  function bindingEditor(initial, { onChange }) {
    const items = initial.map((b) => structuredClone(b));
    let editing = null;
    const rows = h("div", { class: "bindings" });
    const formHost = h("div", { class: "binding-form-host" });
    const add = h("button", { class: "add-binding", onclick: () => edit(null) }, "+ Add hard switch");
    const changed = (b) => {
      const before = initial.find((o) => o.id && o.id === b.id);
      return !before || acts(before) !== acts(b);
    };

    function draw() {
      fill(
        rows,
        items.length
          ? h(
              "table",
              { class: "bindings-table" },
              h(
                "tbody",
                {},
                items.map((b, i) =>
                  h(
                    "tr",
                    { class: "binding", dataset: { binding: b.id || "" } },
                    h(
                      "td",
                      {},
                      h("strong", {}, b.name),
                      changed(b) ? h("span", { class: "muted" }, " (unsaved)") : null,
                    ),
                    h("td", { class: "source" }, sourceText(b.source)),
                    h("td", { class: "mode" }, modeText(b)),
                    h(
                      "td",
                      { class: "actions" },
                      h(
                        "button",
                        { class: "link edit-binding", disabled: editing !== null, onclick: () => edit(i) },
                        "Edit",
                      ),
                      h(
                        "button",
                        {
                          class: "link remove-binding",
                          disabled: editing !== null,
                          onclick: () => {
                            items.splice(i, 1);
                            draw();
                            onChange();
                          },
                        },
                        "Remove",
                      ),
                    ),
                  ),
                ),
              ),
            )
          : h("p", { class: "muted none" }, "No hard switches bound."),
      );
      add.hidden = editing !== null;
    }

    function edit(index) {
      editing = index === null ? "new" : index;
      const finish = () => {
        editing = null;
        formHost.replaceChildren();
        draw();
        onChange();
      };
      fill(
        formHost,
        bindingForm(index === null ? null : items[index], {
          done: (binding) => {
            if (index === null) items.push(binding);
            else items[index] = binding;
            finish();
          },
          cancel: finish,
        }),
      );
      draw();
      onChange();
    }

    draw();
    return {
      el: h("div", { class: "binding-editor" }, rows, formHost, add),
      value: () => items.map((b) => structuredClone(b)),
      items: () => items,
      changed,
      editing: () => editing !== null,
    };
  }

  // One binding: name, source (entity and event first; device trigger under Advanced),
  // and mode. Checked here for the user's sake; the server checks it again on save.
  function bindingForm(binding, { done, cancel }) {
    formSerial += 1;
    const typeName = `source-type-${formSerial}`;
    const modeName = `mode-${formSerial}`;
    const src = binding?.source || { type: "state" };
    const name = h("input", {
      type: "text",
      class: "binding-name",
      value: binding?.name || "",
      placeholder: "e.g. Hall wall switch",
    });
    const entity = h("input", {
      type: "text",
      class: "source-entity",
      list: "lm-entities",
      value: src.type === "state" ? src.entity_id : "",
      placeholder: "switch.hall_wall",
      autocomplete: "off",
    });
    const eventType = h("input", {
      type: "text",
      class: "source-event-type",
      list: "lm-event-types",
      value: src.event_type || "",
      placeholder: "zha_event",
      autocomplete: "off",
    });
    const eventData = h("textarea", {
      class: "source-event-data",
      placeholder: '{"device_ieee": "…", "command": "on"}',
    });
    eventData.value = src.event_data ? JSON.stringify(src.event_data, null, 2) : "";
    const deviceId = h("input", {
      type: "text",
      class: "source-device-id",
      value: src.device_id || "",
      autocomplete: "off",
    });
    const trigger = h("textarea", {
      class: "source-trigger",
      placeholder: '{"domain": "zha", "type": "remote_button_short_press", "subtype": "turn_on"}',
    });
    trigger.value = src.trigger ? JSON.stringify(src.trigger, null, 2) : "";
    const turn = h(
      "select",
      { class: "turn" },
      h("option", { value: "off" }, "turn its bulbs off"),
      h("option", { value: "on" }, "turn its bulbs on"),
    );
    turn.value = binding?.turn || "off";
    const error = h("p", { class: "form-error", role: "alert", hidden: true });

    const typeRadio = (value, label) =>
      h(
        "label",
        { class: "radio" },
        h("input", { type: "radio", name: typeName, value, checked: src.type === value, onchange: sync }),
        ` ${label}`,
      );
    const modeRadio = (value) =>
      h(
        "label",
        { class: "radio mode-option" },
        h("input", {
          type: "radio",
          name: modeName,
          value,
          checked: (binding?.mode || "toggle") === value,
          onchange: sync,
        }),
        h("span", {}, h("strong", {}, ` ${value}`), h("span", { class: "muted" }, ` — ${MODES[value]}`)),
      );

    const entityHint = state.entitiesError
      ? `Couldn’t load Home Assistant’s entities (${state.entitiesError}): type the entity id.`
      : "A switch, binary sensor, light, input boolean or event entity from Home Assistant, or type any entity id.";
    const stateFields = h(
      "div",
      { class: "source-fields state-fields" },
      h("label", { class: "field" }, h("span", {}, "Entity"), entity, h("small", { class: "muted" }, entityHint)),
    );
    const eventFields = h(
      "div",
      { class: "source-fields event-fields" },
      h("label", { class: "field" }, h("span", {}, "Event type"), eventType),
      h(
        "label",
        { class: "field" },
        h("span", {}, "Event data (optional JSON object: must match a subset of the event’s data)"),
        eventData,
      ),
    );
    const deviceFields = h(
      "div",
      { class: "source-fields device-fields" },
      h("label", { class: "field" }, h("span", {}, "Device id"), deviceId),
      h("label", { class: "field" }, h("span", {}, "Trigger (JSON: domain, type, subtype, …)"), trigger),
    );
    const turnField = h("label", { class: "field turn-field" }, h("span", {}, "Each press will"), turn);
    const advanced = h(
      "details",
      { class: "advanced", open: src.type === "device" },
      h("summary", {}, "Advanced: device trigger"),
      h(
        "p",
        { class: "muted" },
        "Only for a button that exposes no entity and fires no event. Copy the device id and the trigger’s fields from an HA automation’s YAML. The device id only reads the press; it never addresses a light.",
      ),
      typeRadio("device", "Device trigger"),
    );

    const form = h(
      "div",
      { class: "binding-form box" },
      h("h4", {}, binding ? `Edit ${binding.name}` : "New hard switch"),
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h(
        "fieldset",
        { class: "source-type" },
        h("legend", {}, "Source"),
        typeRadio("state", "Entity (state)"),
        typeRadio("event", "HA event"),
        advanced,
      ),
      stateFields,
      eventFields,
      deviceFields,
      h("fieldset", { class: "mode" }, h("legend", {}, "Mode"), ["toggle", "press", "follow"].map(modeRadio)),
      turnField,
      error,
      h(
        "div",
        { class: "form-actions" },
        h("button", { class: "cancel-binding", onclick: cancel }, "Cancel"),
        h("button", { class: "primary done-binding", onclick: submit }, binding ? "Update" : "Add"),
      ),
    );

    const picked = (group) => form.querySelector(`input[name="${group}"]:checked`)?.value;

    function sync() {
      const type = picked(typeName);
      stateFields.hidden = type !== "state";
      eventFields.hidden = type !== "event";
      deviceFields.hidden = type !== "device";
      const follow = form.querySelector(`input[name="${modeName}"][value="follow"]`);
      follow.disabled = type !== "state";
      if (follow.checked && type !== "state")
        form.querySelector(`input[name="${modeName}"][value="toggle"]`).checked = true;
      turnField.hidden = picked(modeName) !== "press";
    }

    function jsonObject(text, what) {
      let value;
      try {
        value = JSON.parse(text);
      } catch {
        throw new Error(`${what} is not valid JSON.`);
      }
      if (!value || typeof value !== "object" || Array.isArray(value))
        throw new Error(`${what} must be a JSON object.`);
      return value;
    }

    function build() {
      const label = name.value.trim();
      if (!label) throw new Error("Give the hard switch a name.");
      const type = picked(typeName);
      const mode = picked(modeName);
      let source;
      if (type === "state") {
        const id = entity.value.trim();
        if (!ENTITY_ID.test(id)) throw new Error("Pick an entity (e.g. switch.hall_wall).");
        if (id.split(".")[1].startsWith("lm_"))
          throw new Error("That is one of Light Map’s own entities: bind the physical switch.");
        if (mode === "toggle" && mappedBulb(id)) {
          throw new Error(
            "A toggle can’t take a bulb Light Map drives as its source: each change would set off the next. Use follow or press.",
          );
        }
        source = { type, entity_id: id };
      } else if (type === "event") {
        const eventTypeValue = eventType.value.trim();
        if (!/^\S+$/.test(eventTypeValue)) throw new Error("Enter the event type the button fires (no spaces).");
        source = { type, event_type: eventTypeValue };
        if (eventData.value.trim()) source.event_data = jsonObject(eventData.value, "Event data");
      } else {
        const device = deviceId.value.trim();
        if (!DEVICE_ID.test(device)) throw new Error("Enter the device id (letters, digits, _ and -).");
        const fields = jsonObject(trigger.value || "null", "The trigger");
        if (typeof fields.domain !== "string" || typeof fields.type !== "string")
          throw new Error("The trigger needs a domain and a type.");
        const reserved = RESERVED_TRIGGER_KEYS.filter((k) => k in fields);
        if (reserved.length) throw new Error(`The trigger may not set ${reserved.join(", ")}.`);
        source = { type, device_id: device, trigger: fields };
      }
      if (mode === "follow" && type !== "state") throw new Error("Follow needs an entity source.");
      const result = { name: label, source, mode };
      if (binding?.id) result.id = binding.id;
      if (mode === "press") result.turn = turn.value;
      return result;
    }

    function submit() {
      try {
        done(build());
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    }

    sync();
    return form;
  }

  // The coexistence warning: shown whenever a save would make a hard switch drive
  // something new, and saving needs its box ticked.
  function coexistenceBox(onChange) {
    const ack = h("input", { type: "checkbox", class: "ack", onchange: onChange });
    const items = h("ul", { class: "plain going-live" });
    const el = h(
      "div",
      { class: "box coexistence", role: "alert", hidden: true },
      h(
        "p",
        {},
        h("span", { class: "badge live" }, "LIVE"),
        " ",
        h("strong", {}, "Saving this makes bulbs live beside ha-adaptive-light."),
      ),
      h(
        "p",
        {},
        "Once deployed, these hard switches drive their bulbs through Light Map’s bridges. Until cutover (P6) the old system keeps running and drives the same bulbs, and neither knows what the other did. Bind only a switch the old system doesn’t use.",
      ),
      items,
      h("label", { class: "ack-label" }, ack, " I understand: make these bulbs live"),
    );
    // What the popup worked out itself (`set`), plus what the server last said would go
    // live (`server`), kept until the next answer: the client's view can be behind the
    // server's, and recomputing from it alone would hide the server's warning again.
    let client = [];
    let server = [];
    let shown = "";
    const draw = () => {
      const lines = [...new Set([...client, ...server])];
      const key = lines.join("\n");
      // A different list needs a fresh acknowledgement: ticking for one binding must not
      // carry over to the next one added.
      if (key !== shown) ack.checked = false;
      shown = key;
      fill(items, ...lines.map((line) => h("li", {}, line)));
      el.hidden = !lines.length;
    };
    return {
      el,
      ack,
      set(lines) {
        client = lines;
        draw();
      },
      server(lines) {
        server = lines;
        draw();
      },
      ok: () => el.hidden || ack.checked,
    };
  }

  // An edit refused because what it was made against has changed since (another tab, or a
  // sync): close the popup, say so, and show the Map as it now is.
  async function afterStale(result) {
    if (!result?.stale) return false;
    notify(result.stale.message, { kind: "error" }); // expected, so not fail(): nothing to log
    await reload();
    return true;
  }

  function goingLiveLines(goingLive, nameOf) {
    return goingLive.map((g) => `${g.name} → ${g.bulbs.map(nameOf).join(", ")}`);
  }

  function bulbNamer() {
    const all = [...state.map.rooms.flatMap((r) => r.bulbs), ...state.map.unassigned.bulbs];
    return (id) => all.find((b) => b.id === id)?.name || id;
  }

  function showError(host, err) {
    fill(
      host,
      h("div", { class: "box error" }, h("p", {}, err.message), err.problems?.length ? list(err.problems) : null),
    );
  }

  function savedNotice(result) {
    const live = result.going_live?.length || 0;
    notify(
      `Saved. Deploy to apply it to Home Assistant.${live ? ` ${plural(live, "hard switch goes", "hard switches go")} live on the next deploy.` : ""}`,
      { kind: "ok", timeout: 8000 },
    );
  }

  async function softPopup(bulb) {
    await ensureEntities();
    const saved = await dialog(
      `Hard switches · ${bulb.name}`,
      (close) => {
        const save = h("button", { class: "primary save" }, "Save");
        const errors = h("div");
        const coexist = coexistenceBox(update);
        const editor = bindingEditor(bulb.hard_switches, { onChange: update });
        function update() {
          coexist.set(
            editor
              .items()
              .filter(editor.changed)
              .map((b) => `${b.name} → ${bulb.name}`),
          );
          save.disabled = editor.editing() || !coexist.ok();
        }
        save.addEventListener("click", async () => {
          save.disabled = true;
          try {
            close(
              await close.during(
                api("PUT", `/api/bulbs/${bulb.id}/hard_switches`, {
                  hard_switches: editor.value(),
                  acknowledge_live: coexist.ack.checked,
                  base: bulb.version,
                }),
              ),
            );
          } catch (err) {
            if (err.error === "edit_stale") return close({ stale: err });
            if (err.error === "coexistence") coexist.server(goingLiveLines(err.body.going_live, bulbNamer()));
            showError(errors, err);
            save.disabled = editor.editing() || !coexist.ok();
          }
        });
        const viaGroups = bulb.live_via.filter((via) => via.owner.kind === "group");
        const body = h(
          "div",
          { class: "soft-popup" },
          h(
            "p",
            {},
            h("code", {}, bulb.soft),
            " is the soft switch for ",
            h("strong", {}, bulb.name),
            " (",
            h("code", {}, bulb.entity_id),
            "): the single source of truth for its power. A hard switch bound here flips it, and the bridge makes the bulb follow.",
          ),
          viaGroups.length
            ? h(
                "p",
                { class: "muted" },
                `Also switched through ${viaGroups.map((v) => `${v.name} (group ${v.owner.name})`).join(", ")}: edit the group to change those.`,
              )
            : null,
          editor.el,
          coexist.el,
          errors,
          footer(h("button", { class: "cancel", onclick: () => close() }, "Cancel"), save),
        );
        update();
        return body;
      },
      { wide: true, name: "hard-switches" },
    );
    if (await afterStale(saved)) return;
    if (saved) {
      savedNotice(saved);
      await reload();
    }
  }

  // -- Switching groups ----------------------------------------------------------------

  async function groupPopup(room, group) {
    await ensureEntities();
    const nameOf = (id) => room.bulbs.find((b) => b.id === id)?.name || id;
    const result = await dialog(
      group ? `Switching group · ${group.name}` : `New switching group · ${room.name}`,
      (close) => {
        const before = new Set(group?.members || []);
        const name = h("input", {
          type: "text",
          class: "group-name-input",
          value: group?.name || "",
          placeholder: "e.g. All living room lights",
          oninput: () => update(),
        });
        const boxes = room.bulbs.map((b) =>
          h(
            "label",
            { class: "member" },
            h("input", { type: "checkbox", value: b.id, checked: before.has(b.id), onchange: () => update() }),
            ` ${b.name} `,
            h("code", { class: "muted" }, b.entity_id),
          ),
        );
        const members = () =>
          boxes
            .map((label) => label.querySelector("input"))
            .filter((i) => i.checked)
            .map((i) => i.value);
        const editor = bindingEditor(group?.hard_switches || [], { onChange: () => update() });
        const coexist = coexistenceBox(() => update());
        const errors = h("div");
        const save = h("button", { class: "primary save" }, group ? "Save" : "Create group");
        function update() {
          const now = members();
          const added = now.filter((m) => !before.has(m));
          const lines = [];
          for (const b of editor.items()) {
            if (editor.changed(b)) lines.push(`${b.name} → ${now.map(nameOf).join(", ") || "no bulbs yet"}`);
            else if (added.length) lines.push(`${b.name} now also drives ${added.map(nameOf).join(", ")}`);
          }
          coexist.set(lines);
          save.disabled = editor.editing() || !coexist.ok() || !name.value.trim() || !now.length;
        }
        save.addEventListener("click", async () => {
          save.disabled = true;
          const body = {
            name: name.value.trim(),
            members: members(),
            hard_switches: editor.value(),
            acknowledge_live: coexist.ack.checked,
            base: group?.version,
          };
          try {
            close(
              await close.during(
                group
                  ? api("PUT", `/api/rooms/${room.id}/groups/${group.id}`, body)
                  : api("POST", `/api/rooms/${room.id}/groups`, body),
              ),
            );
          } catch (err) {
            if (err.error === "edit_stale") return close({ stale: err });
            if (err.error === "coexistence") coexist.server(goingLiveLines(err.body.going_live, nameOf));
            showError(errors, err);
            update();
          }
        });
        const remove = async () => {
          const sure = await confirmBox(
            `Delete ${group.name}?`,
            [
              group.hard_switches.length
                ? `Its ${plural(group.hard_switches.length, "hard switch", "hard switches")} go with it; the next deploy removes their automations. The bulbs themselves are untouched.`
                : "The bulbs themselves are untouched.",
            ],
            { confirm: "Delete group", danger: true },
          );
          if (!sure) return;
          try {
            await close.during(
              api("DELETE", `/api/rooms/${room.id}/groups/${group.id}?base=${encodeURIComponent(group.version)}`),
            );
            close({ deleted: true });
          } catch (err) {
            if (err.error === "edit_stale") return close({ stale: err });
            showError(errors, err);
          }
        };
        const body = h(
          "div",
          { class: "group-popup" },
          h(
            "p",
            { class: "muted" },
            "A switching group is a named set of this room’s bulbs plus the hard switches that switch them together. It has no entity of its own in Home Assistant: a hard switch bound here acts on every member’s soft switch.",
          ),
          h("label", { class: "field" }, h("span", {}, "Name"), name),
          h("fieldset", { class: "members" }, h("legend", {}, "Member bulbs"), boxes),
          h("h3", {}, "Hard switches"),
          editor.el,
          coexist.el,
          errors,
          footer(
            group ? h("button", { class: "danger delete-group", onclick: remove }, "Delete group") : null,
            h("span", { class: "spacer" }),
            h("button", { class: "cancel", onclick: () => close() }, "Cancel"),
            save,
          ),
        );
        update();
        return body;
      },
      { wide: true, name: "group" },
    );
    if (await afterStale(result)) return;
    if (result?.deleted) {
      notify(`Deleted ${group.name}. Deploy to remove its automations from Home Assistant.`, {
        kind: "ok",
        timeout: 8000,
      });
      await reload();
    } else if (result) {
      savedNotice(result);
      await reload();
    }
  }

  // -- Rooms and moves -----------------------------------------------------------------

  async function roomNameDialog(room) {
    const done = await dialog(
      room ? `Rename ${room.name}` : "New room",
      (close) => {
        const input = h("input", { type: "text", class: "room-name-input", value: room?.name || "" });
        const errors = h("div");
        const save = h("button", { class: "primary save" }, room ? "Rename" : "Create");
        const submit = async () => {
          save.disabled = true;
          try {
            close(
              await close.during(
                room
                  ? api("PUT", `/api/rooms/${room.id}/name`, { name: input.value })
                  : api("POST", "/api/rooms", { name: input.value }),
              ),
            );
          } catch (err) {
            showError(errors, err);
            save.disabled = false;
          }
        };
        save.addEventListener("click", submit);
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") submit();
        });
        return h(
          "div",
          {},
          h(
            "p",
            {},
            room
              ? "Renames the room’s Area in Home Assistant (verified). The room keeps its id."
              : "Creates an Area in Home Assistant (verified) and adds it to the Map as a room.",
          ),
          h("label", { class: "field" }, h("span", {}, "Name"), input),
          errors,
          footer(h("button", { class: "cancel", onclick: () => close() }, "Cancel"), save),
        );
      },
      { name: "room" },
    );
    if (done) {
      notify(room ? `Renamed to ${done.room.name}.` : `Created ${done.room.name}.`, { kind: "ok", timeout: 5000 });
      await reload();
    }
  }

  async function toggleAdaptive(room, checkbox) {
    const adaptive = checkbox.checked;
    let discard = false;
    if (!adaptive && room.adaptive_drops.length) {
      discard = await confirmBox(
        `Turn adaptive off for ${room.name}?`,
        [
          "This drops the room’s adaptive config:",
          list(room.adaptive_drops),
          "Its switching plane (soft switches, hard switches, groups) is unaffected.",
        ],
        { confirm: "Drop it", danger: true },
      );
      if (!discard) {
        checkbox.checked = true;
        return;
      }
    }
    checkbox.disabled = true;
    try {
      await api("PUT", `/api/rooms/${room.id}/adaptive`, {
        adaptive,
        discard_adaptive_config: discard,
        base: room.version,
      });
      await reload();
    } catch (err) {
      checkbox.checked = !adaptive;
      checkbox.disabled = false;
      if (err.error === "edit_stale") await reload();
      throw err;
    }
  }

  async function moveDialog(bulb, room) {
    const targets = state.map.rooms.filter((r) => r.id !== room?.id).map((r) => [r.id, r.name]);
    if (room) targets.push(["", "Unassigned"]);
    const moved = await dialog(
      `Move ${bulb.name}`,
      (close) => {
        const select = h(
          "select",
          { class: "move-target" },
          h("option", { value: "-", disabled: true, selected: true }, "Choose a room…"),
          targets.map(([id, name]) => h("option", { value: id }, name)),
        );
        const out = h("div", { class: "move-preview" });
        const go = h("button", { class: "primary confirm-move", disabled: true }, "Move");
        let preview = null;
        let target = null;
        let asked = 0;
        select.addEventListener("change", async () => {
          target = select.value === "" ? null : select.value;
          go.disabled = true;
          preview = null;
          fill(out, h("p", { class: "muted" }, "Checking…"));
          // Only the answer for the latest choice counts: picking A then B quickly must not
          // show A's preview under B (the server would refuse its fingerprint anyway).
          const mine = ++asked;
          let answer;
          try {
            answer = await api("POST", `/api/bulbs/${bulb.id}/move/preview`, { room_id: target });
          } catch (err) {
            if (mine === asked) showError(out, err);
            return;
          }
          if (mine !== asked) return;
          preview = answer;
          fill(
            out,
            h("h3", {}, "What changes"),
            list(preview.changes),
            preview.refused
              ? h(
                  "div",
                  { class: "box error refused" },
                  h("p", {}, "Home Assistant would refuse this move:"),
                  h("p", {}, preview.refused),
                )
              : null,
            preview.problems.length
              ? h("div", { class: "box error" }, h("p", {}, "The result would not validate:"), list(preview.problems))
              : null,
          );
          go.disabled = Boolean(preview.refused || preview.problems.length);
        });
        go.addEventListener("click", async () => {
          go.disabled = true;
          try {
            close(
              await close.during(
                api("POST", `/api/bulbs/${bulb.id}/move`, { room_id: target, fingerprint: preview.fingerprint }),
              ),
            );
          } catch (err) {
            showError(out, err);
          }
        });
        return h(
          "div",
          {},
          h(
            "p",
            {},
            "Moving ",
            h("strong", {}, bulb.name),
            " writes its Area to Home Assistant first (verified), then records the move in the snapshot. Its soft switch and hard switches move with it; the room it leaves drops it from its switching groups.",
          ),
          h("label", { class: "field" }, h("span", {}, "Move to"), select),
          out,
          footer(h("button", { class: "cancel", onclick: () => close() }, "Cancel"), go),
        );
      },
      { name: "move" },
    );
    if (moved) {
      notify(`Moved ${bulb.name}.`, { kind: "ok", timeout: 5000 });
      await reload();
    }
  }

  // -- Sync from HA --------------------------------------------------------------------

  function setSyncPill(preview) {
    const pill = document.getElementById("sync-state");
    if (!pill) return;
    pill.title = "";
    if (preview.state === "synced") {
      pill.textContent = "Synced with HA";
      pill.className = "pill ok";
    } else if (preview.state === "unsaved") {
      pill.textContent = "Not saved yet";
      pill.className = "pill warn";
    } else {
      pill.textContent = `Stale: ${plural(preview.changes.length, "change")}`;
      pill.className = "pill warn";
      pill.title = preview.changes.join("\n");
    }
  }

  async function refreshSync() {
    const pill = document.getElementById("sync-state");
    if (!pill) return;
    try {
      setSyncPill(await api("GET", "/api/sync/preview"));
    } catch (err) {
      pill.textContent = err.error === "ha_not_configured" ? "HA not configured" : "HA unreachable";
      pill.className = "pill error";
      pill.title = err.message;
    }
  }

  async function syncDialog() {
    const saved = await dialog(
      "Sync from Home Assistant",
      (close) => {
        const out = h("div", {}, h("p", { class: "muted" }, "Reading Home Assistant’s Areas…"));
        const errors = h("div");
        const save = h("button", { class: "primary save-sync", disabled: true }, "Save snapshot");
        let preview = null;
        const load = async (note) => {
          save.disabled = true;
          try {
            preview = await api("GET", "/api/sync/preview");
          } catch (err) {
            showError(out, err);
            return;
          }
          setSyncPill(preview);
          fill(
            out,
            note ? h("div", { class: "box warn" }, note) : null,
            h(
              "p",
              {},
              "Home Assistant’s Areas decide which rooms exist and which bulbs are in them. The Map works from a committed snapshot of them; this is what re-reading HA would change in it. Nothing is saved until you save it.",
            ),
            preview.changes.length
              ? [h("h3", {}, plural(preview.changes.length, "change")), list(preview.changes)]
              : h("div", { class: "box ok" }, "Home Assistant matches the snapshot."),
            preview.problems.length
              ? h(
                  "div",
                  { class: "box error" },
                  h("p", {}, "The synced snapshot would not validate:"),
                  list(preview.problems),
                )
              : null,
          );
          save.disabled = Boolean(preview.problems.length) || preview.state === "synced";
        };
        save.addEventListener("click", async () => {
          save.disabled = true;
          errors.replaceChildren();
          try {
            close(await close.during(api("POST", "/api/sync", { fingerprint: preview.fingerprint })));
          } catch (err) {
            if (err.error === "preview_stale")
              await load("Home Assistant changed while you were reviewing: this is the new preview.");
            else showError(errors, err);
          }
        });
        load();
        return h(
          "div",
          {},
          out,
          errors,
          footer(h("button", { class: "cancel", onclick: () => close() }, "Cancel"), save),
        );
      },
      { wide: true, name: "sync" },
    );
    if (saved) {
      notify(`Saved the snapshot (${plural(saved.changes.length, "change")}).`, { kind: "ok", timeout: 5000 });
      await reload();
    }
  }

  // -- Deploy --------------------------------------------------------------------------

  const KIND_LABEL = { input_boolean: "soft switch", input_select: "select", automation: "automation" };

  function planRows(plan) {
    const rows = [];
    for (const [label, verb] of [
      ["created", "create"],
      ["updated", "update"],
      ["removed", "remove"],
    ]) {
      for (const [kind, ids] of Object.entries(plan[label])) {
        for (const id of ids) rows.push([verb, kind, id]);
      }
    }
    return rows;
  }

  function crossoverSection(report) {
    const parts = [h("h3", {}, "Crossover report (read-only)")];
    if (report.drivers.length) {
      parts.push(
        h(
          "p",
          {},
          "Already driving these bulbs in Home Assistant. Light Map leaves them alone, and until cutover they keep running beside it:",
        ),
        h(
          "table",
          { class: "plan-table drivers" },
          h(
            "tbody",
            {},
            report.drivers.map((d) =>
              h(
                "tr",
                { class: d.adaptive_light ? "driver adaptive-light" : "driver" },
                h("td", {}, d.name, d.adaptive_light ? h("span", { class: "pill warn" }, "ha-adaptive-light") : null),
                h("td", {}, h("code", {}, d.entity_id)),
                h("td", { class: "muted" }, d.kind),
                h("td", {}, d.bulbs.join(", ")),
              ),
            ),
          ),
        ),
      );
    } else if (!report.errors.length) {
      parts.push(h("p", { class: "muted" }, "No other automation, script or scene names these bulbs."));
    }
    if (report.groups.length) {
      parts.push(
        h(
          "p",
          {},
          "Home Assistant groups with these bulbs among their members (commanding the group drives them too):",
        ),
        list(report.groups, (g) => [h("code", {}, g.entity_id), ` ${g.name}: ${g.bulbs.join(", ")}`]),
      );
    }
    if (report.collisions.length) {
      parts.push(
        h(
          "div",
          { class: "box error collisions" },
          h("p", {}, h("strong", {}, "Name collisions")),
          list(report.collisions),
        ),
      );
    }
    if (report.errors.length) parts.push(h("div", { class: "box warn" }, list(report.errors)));
    parts.push(
      h(
        "p",
        { class: "muted small" },
        "Found through Home Assistant’s search, by entity id: an automation that targets a whole Area or device isn’t listed.",
      ),
    );
    return parts;
  }

  function deployPreview(preview) {
    const parts = [
      h(
        "div",
        { class: "box warn coexistence-note" },
        h("p", {}, h("strong", {}, "Coexistence. "), preview.coexistence.note),
        h(
          "p",
          {},
          preview.coexistence.live_bulbs.length
            ? `Live bulbs: ${preview.coexistence.live_bulbs.join(", ")}.`
            : "No bulb is live: no hard switch is bound, so the new bridges stay inert until something flips a soft switch.",
        ),
      ),
    ];
    if (!preview.ready) {
      parts.push(
        h(
          "div",
          { class: "box error refused" },
          h("p", {}, h("strong", {}, "This deploy would be refused before writing anything:")),
          list(preview.refused),
        ),
      );
    } else {
      const rows = planRows(preview.plan);
      parts.push(h("h3", {}, preview.modules ? "Plan: the lm_ lane, first" : "Plan: the lm_ lane only"));
      if (!rows.length) {
        const nothing = preview.modules
          ? "The lm_ lane already matches the config: nothing to do there."
          : "Home Assistant already matches the config: nothing to deploy.";
        parts.push(h("div", { class: "box ok nothing" }, nothing));
      } else {
        parts.push(
          h(
            "table",
            { class: "plan-table steps" },
            h(
              "tbody",
              {},
              rows.map(([verb, kind, id]) =>
                h(
                  "tr",
                  { class: `step ${verb}` },
                  h("td", { class: "verb" }, verb),
                  h("td", { class: "muted" }, KIND_LABEL[kind] || kind),
                  h("td", {}, h("code", {}, id)),
                ),
              ),
            ),
          ),
        );
      }
      if (preview.plan.sync.length) {
        parts.push(
          h(
            "p",
            {},
            "Before each new bridge goes live, its soft switch is set to its bulb’s current on/off, so nothing is driven:",
          ),
          list(preview.plan.sync),
        );
      }
      if (preview.plan.warnings.length) parts.push(h("div", { class: "box warn" }, list(preview.plan.warnings)));
      if (preview.going_live.length) {
        parts.push(
          h("h3", {}, "Hard switches going live"),
          h(
            "div",
            { class: "box warn going-live" },
            list(preview.going_live, (g) => `${g.name} (${g.automation}, ${g.op}) drives ${g.bulbs.join(", ")}`),
          ),
        );
      }
    }
    return [...parts, ...modulesSection(preview.modules), ...crossoverSection(preview.crossover)];
  }

  // Module lanes (the adaptive al_ lane), present only once the cutover switch wires one in
  // (P6a): the deploy applies each one shown ready after the lm_ lane, and one refused here
  // not at all, so the next preview shows its plan.
  function modulesSection(modules) {
    if (!modules) return [];
    const parts = [h("h3", {}, "Module lanes, after lm_"), h("p", { class: "muted" }, modules.note)];
    for (const [prefix, lane] of Object.entries(modules.lanes)) {
      if (!lane.ready) {
        parts.push(
          h(
            "div",
            { class: "box warn module-lane" },
            h("p", {}, `${prefix}_ would be refused, so this deploy won’t apply it:`),
            list(lane.refused),
          ),
        );
        continue;
      }
      const rows = planRows(lane.plan);
      parts.push(
        h(
          "div",
          { class: "box module-lane" },
          h("p", {}, `${prefix}_: ${rows.length ? `${plural(rows.length, "step")}, applied after lm_` : "nothing to do"}`),
          rows.length ? list(rows, ([verb, kind, id]) => `${verb} ${KIND_LABEL[kind] || kind} ${id}`) : null,
        ),
      );
    }
    return parts;
  }

  // The steps a deploy would take: the lm_ lane's, and each ready module lane's.
  function deploySteps(preview) {
    if (!preview?.ready) return 0;
    const lanes = Object.values(preview.modules?.lanes || {}).filter((lane) => lane.ready);
    return [preview.plan, ...lanes.map((lane) => lane.plan)].reduce((n, plan) => n + planRows(plan).length, 0);
  }

  // What each module lane did, under the lm_ lane's result (only with the cutover switch on).
  function modulesResult(modules) {
    if (!modules) return [];
    return Object.entries(modules).map(([prefix, lane]) =>
      lane.deployed
        ? deployResult(lane.result, `The ${prefix}_ lane: deployed and verified.`, "module-result")
        : h(
            "div",
            { class: "box warn module-result" },
            h("p", {}, `${prefix}_ was not applied: it was refused in the preview. Preview again to see its plan.`),
            list(lane.refused),
          ),
    );
  }

  function deployResult(result, title = "Deployed and verified.", extra = null) {
    const count = (label) => Object.values(result[label]).reduce((n, ids) => n + ids.length, 0);
    const unsynced = Object.entries(result.unsynced);
    return h(
      "div",
      { class: extra ? `box ok deploy-result ${extra}` : "box ok deploy-result" },
      h(
        "p",
        {},
        h("strong", {}, title),
        ` Created ${count("created")}, updated ${count("updated")}, removed ${count("removed")}.`,
      ),
      Object.keys(result.synced).length
        ? h("p", {}, `Soft switches set to their bulbs’ state: ${Object.keys(result.synced).join(", ")}.`)
        : null,
      unsynced.length
        ? h("p", {}, `Left as they were (bulb not on or off): ${unsynced.map(([s, b]) => `${s} (${b})`).join(", ")}.`)
        : null,
    );
  }

  async function deployDialog() {
    await dialog(
      "Deploy the switching plane",
      (close) => {
        const out = h("div", {}, h("p", { class: "muted" }, "Planning…"));
        const errors = h("div");
        const ack = h("input", { type: "checkbox", class: "ack" });
        const ackLabel = h(
          "label",
          { class: "ack-label box warn", hidden: true },
          ack,
          " I understand: these hard switches go live beside ha-adaptive-light",
        );
        const go = h("button", { class: "primary deploy-confirm", disabled: true }, "Deploy to Home Assistant");
        const cancel = h("button", { class: "cancel", onclick: () => close() }, "Cancel");
        const intro = h(
          "p",
          {},
          "Creates, updates and removes Light Map’s soft switches and automations (the lm_ lane) in Home Assistant, then re-reads HA to verify. It never touches anything outside that lane, never writes Areas, and never calls a light service.",
        );
        let preview = null;
        const update = () => {
          const steps = deploySteps(preview);
          go.disabled = !steps || (preview.going_live.length > 0 && !ack.checked);
        };
        ack.addEventListener("change", update);
        const load = async (note) => {
          go.disabled = true;
          try {
            preview = await api("GET", "/api/deploy/preview");
          } catch (err) {
            showError(out, err);
            return;
          }
          fill(out, note ? h("div", { class: "box warn" }, note) : null, ...deployPreview(preview));
          if (preview.modules) {
            // The cutover switch on (P6a): the module lanes are deployed too, after lm_.
            intro.textContent =
              "Creates, updates and removes Light Map’s soft switches and automations (the lm_ lane), then each module lane’s helpers and automations, in Home Assistant, re-reading HA to verify each lane before the next. It never touches anything outside Light Map’s lanes, never writes Areas, and never calls a light service.";
          }
          ackLabel.hidden = !(preview.ready && preview.going_live.length);
          ack.checked = false;
          update();
        };
        go.addEventListener("click", async () => {
          go.disabled = true;
          go.textContent = "Deploying…";
          errors.replaceChildren();
          try {
            const done = await close.during(api("POST", "/api/deploy", { fingerprint: preview.fingerprint }));
            fill(out, deployResult(done.result), ...modulesResult(done.modules));
            go.hidden = true;
            ackLabel.hidden = true;
            cancel.textContent = "Close";
          } catch (err) {
            go.textContent = "Deploy to Home Assistant";
            if (err.error === "preview_stale")
              // A module lane found stale once lm_ was written says what was deployed.
              await load(
                err.body?.deployed
                  ? err.message
                  : "What a deploy would do changed while you were reviewing: this is the new preview.",
              );
            else {
              showError(errors, err);
              update();
            }
          }
        });
        load();
        return h(
          "div",
          { class: "deploy-preview" },
          intro,
          out,
          errors,
          ackLabel,
          footer(cancel, go),
        );
      },
      { wide: true, name: "deploy" },
    );
  }

  // -- Area writes ---------------------------------------------------------------------

  async function areasDialog() {
    const written = await dialog(
      "Write Areas to Home Assistant",
      (close) => {
        const out = h("div", {}, h("p", { class: "muted" }, "Comparing the snapshot with HA’s Areas…"));
        const errors = h("div");
        const go = h("button", { class: "danger areas-confirm", disabled: true }, "Write Areas");
        let preview = null;
        const load = async (note) => {
          go.disabled = true;
          try {
            preview = await api("GET", "/api/areas/preview");
          } catch (err) {
            showError(out, err);
            return;
          }
          fill(
            out,
            note ? h("div", { class: "box warn" }, note) : null,
            h("div", { class: "box warn" }, preview.note),
            preview.moves.length
              ? h(
                  "table",
                  { class: "plan-table moves" },
                  h(
                    "tbody",
                    {},
                    preview.moves.map((m) =>
                      h(
                        "tr",
                        { class: "area-move" },
                        h("td", {}, h("code", {}, m.entity_id)),
                        h("td", {}, `${m.from_name} → ${m.to_name}`),
                      ),
                    ),
                  ),
                )
              : h("div", { class: "box ok" }, "Home Assistant’s Areas already match the snapshot: nothing to write."),
            preview.problems.length
              ? h("div", { class: "box error" }, h("p", {}, "Refused before writing anything:"), list(preview.problems))
              : null,
          );
          go.disabled = !preview.ready;
        };
        go.addEventListener("click", async () => {
          go.disabled = true;
          errors.replaceChildren();
          try {
            close(await close.during(api("POST", "/api/areas/deploy", { fingerprint: preview.fingerprint })));
          } catch (err) {
            if (err.error === "preview_stale")
              await load("Home Assistant changed while you were reviewing: this is the new preview.");
            else if (err.error === "areas_partial") {
              // Part of it landed: what is left to write is a new plan, so show that.
              await load("Part of the Area writes landed before they stopped: this is what is left.");
              showError(errors, err);
            } else showError(errors, err);
          }
        });
        load();
        return h(
          "div",
          {},
          out,
          errors,
          footer(h("button", { class: "cancel", onclick: () => close() }, "Cancel"), go),
        );
      },
      { wide: true, name: "areas" },
    );
    if (written) {
      notify(`Wrote ${plural(written.moves.length, "Area move")} to Home Assistant.`, { kind: "ok", timeout: 5000 });
    }
    await reload(); // HA's Areas may have changed even if this didn't finish: re-check the sync state
  }

  // -- Helpers -------------------------------------------------------------------------

  function plural(n, singular, pluralForm = `${singular}s`) {
    return `${n} ${n === 1 ? singular : pluralForm}`;
  }
})();
