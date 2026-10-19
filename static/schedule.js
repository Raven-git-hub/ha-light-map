// The schedule-profile editor (P5.2b): when each section of the day starts, for the rooms
// that follow each profile. #schedule (one profile at a time, #schedule/<id>), under the
// Config tab, reached from the adaptive rooms list, the room editor's profile picker and
// the Now tab.
//
// It works on a draft of the whole global schedule_profiles list and saves it whole (PUT
// /api/schedule_profiles) against the version it was shown, like every other edit: a save
// from a stale editor is refused (edit_stale) and the editor reloads as things now are. The
// version also covers which profile each adaptive room selects, since the editor names the
// rooms a profile is shared with and deletes only a profile no room uses. `default` is
// always kept (the schema requires it). The six sections are always there, in canonical
// order; a section's trigger is a clock time, sunrise or sunset with an offset, or the
// earlier or later of two or more of those; its priority is the trigger's default unless
// set. As the draft changes the editor asks the server to check it (POST
// /api/schedule_profiles/check, which writes nothing): what saving would refuse, and each
// profile's day today at HA's location, so a collision shows as it is made.
//
// Sections are labelled by their canonical names, and a section's own `name` is neither
// shown nor set (display names are deferred: the generated templates read the section id).
// Read from and written through /api; every name is set as text, never as markup. Nothing
// here writes HA: it changes the config only.

"use strict";

(() => {
  const { h, fill, api, notify, fail, confirmBox, SECTIONS, sectionName, plural } = LM;

  const TIME = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
  const TRIGGER_TYPES = [
    ["clock", "At a time"],
    ["sun", "Sunrise or sunset"],
    ["earliest", "The earlier of"],
    ["latest", "The later of"],
  ];
  const COMPOSITE = ["earliest", "latest"];
  const POLICIES = [
    ["collapse", "Collapse", "the lower-priority section is skipped that day."],
    [
      "clamp",
      "Clamp",
      "a section that starts too soon after the one before is moved later, to start the shortest section’s length after it (one that can’t fit that day, or that would run out of order, is still skipped).",
    ],
  ];

  // The editor's state: `view` as the server sent it, `draft` the list being edited,
  // `baseline` the saved list's key (what Save compares with), `selected` the profile on
  // screen, `bad` the fields holding something invalid ({message, raw}: what was typed,
  // shown again when the field is redrawn), `days` and `location` the last check's day per
  // profile, `checked` the last check's problems, `serverProblems` what the last refused
  // save said.
  let sx = null;
  let checkTimer = null;
  // Bumped by every edit, so a check still in flight for an older draft is never applied
  // to a newer one.
  let checkSeq = 0;
  // A failing check is said once, not at every edit, until one succeeds.
  let checkFailing = false;
  // The control to focus after the next redraw, when it isn't the one focused now (a new
  // profile's name field).
  let pendingFocus = null;

  LM.view("schedule", render, { tab: "config" });

  async function render(main, profileId) {
    main.append(
      h(
        "section",
        { id: "schedule-editor", class: "schedule-editor" },
        nav(),
        h("p", { class: "muted" }, "Loading the schedule profiles…"),
      ),
    );
    // A save in flight lands whether or not the view stays.
    LM.protect(() => Boolean(sx && edited() && !sx.saving), "Your schedule changes");
    await load(profileId);
  }

  function nav() {
    return h(
      "nav",
      { class: "editor-nav" },
      h("a", { class: "back-to-config", href: "#config" }, "← Adaptive rooms"),
      h("a", { href: "#map" }, "The Map"),
    );
  }

  function host() {
    return document.getElementById("schedule-editor");
  }

  async function load(profileId) {
    const el = host();
    if (!el) return;
    // Inert until redrawn: an edit typed while the editor reloads (after a save) would be
    // lost, or land on a draft that is gone.
    el.inert = true;
    sx = null;
    clearTimeout(checkTimer);
    let view;
    try {
      view = await api("GET", "/api/schedule_profiles");
    } catch (err) {
      el.inert = false;
      if (el.isConnected) fill(el, nav(), h("div", { class: "box error editor-unavailable" }, err.message));
      fail(err);
      return;
    }
    if (!el.isConnected) return; // another view is showing now
    const profiles = view.schedule_profiles;
    sx = {
      view,
      draft: structuredClone(profiles),
      baseline: canon(profiles),
      selected: profiles.some((p) => p.id === profileId) ? profileId : view.default_profile,
      bad: {},
      days: view.days,
      location: view.location,
      checked: [],
      serverProblems: [],
      saving: false,
      // Per trigger, the shapes it has had: switching its type back restores the one before.
      shapes: {},
    };
    draw();
  }

  // -- The draft -----------------------------------------------------------------------

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

  // dirty: what Save would change; edited: that, or a field holding something not yet in
  // the draft (invalid), which Revert drops and leaving asks about.
  const dirty = () => canon(sx.draft) !== sx.baseline;
  const edited = () => dirty() || Object.keys(sx.bad).length > 0;
  const profileOf = (id) => sx.draft.find((p) => p.id === id);
  const selected = () => profileOf(sx.selected) || profileOf(sx.view.default_profile) || sx.draft[0];
  const label = (profile) => profile.name || profile.id;
  const usedBy = (id) => sx.view.rooms.filter((r) => r.profile === id);
  const isDefault = (profile) => profile.id === sx.view.default_profile;

  function problems() {
    return [...Object.values(sx.bad).map((b) => b.message), ...sx.checked, ...sx.serverProblems];
  }

  // Profile ids are slugs, set once from the name: rooms select a profile by id, so it
  // stays when the profile is renamed.
  function newId(name) {
    const base =
      String(name)
        .normalize("NFKD") // "Café" -> "cafe", not "caf"
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "") || "profile";
    const taken = new Set(sx.draft.map((p) => p.id));
    if (!taken.has(base)) return base;
    let n = 2;
    while (taken.has(`${base}_${n}`)) n += 1;
    return `${base}_${n}`;
  }

  function forget(prefix) {
    for (const key of Object.keys(sx.bad)) if (key.startsWith(prefix)) delete sx.bad[key];
  }

  // After any edit: the server's last refusal no longer describes the draft, and the check
  // is asked again.
  function changed({ full = false } = {}) {
    if (!sx) return;
    sx.serverProblems = [];
    if (full) draw();
    else refreshBar();
    scheduleCheck();
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    const seq = ++checkSeq;
    // A field holding something invalid isn't in the draft: checked now, the draft would
    // pass on the value before it. Its own error stands until it is fixed.
    if (Object.keys(sx.bad).length) return;
    const mine = sx;
    checkTimer = setTimeout(
      () =>
        check(mine, seq).catch((err) => {
          if (!checkFailing) fail(err);
          checkFailing = true;
        }),
      300,
    );
  }

  async function check(mine, seq) {
    const result = await api("POST", "/api/schedule_profiles/check", { schedule_profiles: mine.draft });
    checkFailing = false;
    if (sx !== mine || seq !== checkSeq) return; // reloaded, or edited since it was sent
    mine.checked = result.problems;
    mine.days = result.days;
    mine.location = result.location;
    refreshDays();
    refreshBar();
  }

  // -- Rendering -----------------------------------------------------------------------

  function draw() {
    const el = sx && host();
    if (!el) return;
    // A redraw replaces every control: the one focused (a trigger's type being arrowed
    // through, a profile moved) is focused again, else the one it names as next.
    const active = document.activeElement?.closest?.("[data-focus]");
    const focus = pendingFocus ? [pendingFocus] : active ? [active.dataset.focus, active.dataset.focusNext] : [];
    pendingFocus = null;
    const ids = new Set(sx.draft.map((p) => p.id));
    for (const key of Object.keys(sx.bad)) if (!ids.has(key.split(":")[0])) delete sx.bad[key];
    const profile = selected();
    sx.selected = profile.id;
    LM.replaceRoute(`#schedule/${encodeURIComponent(profile.id)}`);
    fill(
      el,
      nav(),
      h("header", { class: "editor-head" }, h("h2", {}, "Schedule profiles")),
      bar(),
      h(
        "p",
        { class: "muted intro" },
        "When each section of the day starts, for every room that follows the profile. A room chooses its profile in its adaptive config. Saving changes the config only: the rooms follow their profile once adaptive runs, at cutover (P6).",
      ),
      h("p", { id: "schedule-location", class: "muted small" }),
      h(
        "div",
        { class: "schedule-layout" },
        h("section", { class: "editor-card profiles-card" }, h("h3", {}, "Profiles"), profileList()),
        h(
          "section",
          { class: "editor-card profile-card", dataset: { profile: profile.id } },
          h("h3", {}, "Profile"),
          profileEditor(profile),
        ),
      ),
    );
    el.inert = false;
    const controls = [...el.querySelectorAll("[data-focus]")];
    for (const key of focus) {
      const again = key && controls.find((c) => c.dataset.focus === key && !c.disabled);
      if (again) {
        again.focus();
        break;
      }
    }
    refreshDays();
    refreshBar();
  }

  function bar() {
    return h(
      "div",
      { class: "editor-bar" },
      h("span", { id: "schedule-state", class: "pill" }),
      h("span", { class: "spacer" }),
      h("button", { id: "schedule-revert", onclick: () => revert().catch(fail) }, "Revert"),
      h("button", { id: "schedule-save", class: "primary", onclick: () => save().catch(fail) }, "Save"),
      h("div", { id: "schedule-problems", role: "alert" }),
    );
  }

  function refreshBar() {
    const state = document.getElementById("schedule-state");
    if (!state || !sx) return;
    const pending = edited();
    const found = problems();
    state.textContent = sx.saving ? "Saving…" : pending ? "Unsaved changes" : "Saved";
    state.className = pending ? "pill warn" : "pill ok";
    document.getElementById("schedule-save").disabled = sx.saving || !dirty() || found.length > 0;
    document.getElementById("schedule-revert").disabled = sx.saving || !pending;
    fill(
      document.getElementById("schedule-problems"),
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

  function profileList() {
    return [
      h(
        "ol",
        { class: "profile-list" },
        sx.draft.map((profile, i) => {
          const rooms = usedBy(profile.id);
          const on = profile.id === sx.selected;
          const blocked = isDefault(profile)
            ? "The default profile is always kept."
            : rooms.length
              ? `${rooms.map((r) => r.name).join(", ")} ${rooms.length === 1 ? "follows" : "follow"} it: choose another profile for ${rooms.length === 1 ? "that room" : "those rooms"} first.`
              : null;
          return h(
            "li",
            { class: `profile-item${on ? " on" : ""}`, dataset: { profile: profile.id } },
            h(
              "button",
              {
                class: "select-profile",
                "aria-current": on ? "true" : null,
                dataset: { focus: `select:${profile.id}` },
                onclick: () => {
                  sx.selected = profile.id;
                  draw();
                },
              },
              h("span", { class: "profile-label" }, label(profile)),
              h("code", { class: "muted" }, profile.id),
              h(
                "span",
                { class: "muted small used-by" },
                rooms.length ? `used by ${plural(rooms.length, "room")}` : "no room uses it",
              ),
            ),
            h(
              "span",
              { class: "profile-actions" },
              h(
                "button",
                {
                  class: "icon move-up",
                  dataset: { focus: `up:${profile.id}`, focusNext: `select:${profile.id}` },
                  "aria-label": `Move ${label(profile)} up`,
                  title: "Move up",
                  disabled: i === 0,
                  onclick: () => move(i, -1),
                },
                "↑",
              ),
              h(
                "button",
                {
                  class: "icon move-down",
                  dataset: { focus: `down:${profile.id}`, focusNext: `select:${profile.id}` },
                  "aria-label": `Move ${label(profile)} down`,
                  title: "Move down",
                  disabled: i === sx.draft.length - 1,
                  onclick: () => move(i, 1),
                },
                "↓",
              ),
              h(
                "button",
                {
                  class: "icon delete-profile",
                  "aria-label": `Delete ${label(profile)}`,
                  title: blocked || "Delete",
                  disabled: Boolean(blocked),
                  onclick: () => remove(profile).catch(fail),
                },
                "×",
              ),
            ),
          );
        }),
      ),
      h("p", { class: "muted small" }, "The room editor offers the profiles in this order."),
      h("button", { id: "new-profile", onclick: () => create().catch(fail) }, "+ New profile"),
    ];
  }

  function move(index, by) {
    const list = sx.draft;
    const [profile] = list.splice(index, 1);
    list.splice(index + by, 0, profile);
    changed({ full: true });
  }

  async function remove(profile) {
    const sure = await confirmBox(
      `Delete ${label(profile)}?`,
      ["No room follows it. It goes from the draft now, and from the config when you save."],
      { confirm: "Delete", danger: true },
    );
    if (!sure || !sx) return;
    sx.draft = sx.draft.filter((p) => p !== profile);
    forget(`${profile.id}:`);
    if (sx.selected === profile.id) sx.selected = sx.view.default_profile;
    pendingFocus = `select:${sx.selected}`;
    changed({ full: true });
  }

  async function create() {
    const current = selected();
    const result = await LM.dialog(
      "New schedule profile",
      (close) => {
        const name = h("input", {
          type: "text",
          class: "new-profile-name",
          placeholder: "Weekend",
          autofocus: true,
          "aria-label": "Name",
        });
        const fromCopy = h("input", { type: "radio", name: "start-from", value: "copy", checked: true });
        const fromStandard = h("input", { type: "radio", name: "start-from", value: "standard" });
        const error = h("p", { class: "form-error", role: "alert", hidden: true });
        const make = () => {
          const text = name.value.trim();
          if (!text) {
            error.textContent = "The profile needs a name.";
            error.hidden = false;
            return;
          }
          close({ name: text, standard: fromStandard.checked });
        };
        name.addEventListener("keydown", (event) => {
          if (event.key === "Enter") make();
        });
        return h(
          "div",
          {},
          h("label", { class: "field" }, h("span", {}, "Name"), name),
          h(
            "fieldset",
            { class: "start-from" },
            h("legend", {}, "Start from"),
            h("label", {}, fromCopy, ` a copy of ${label(current)}`),
            h(
              "label",
              {},
              fromStandard,
              " the standard schedule (sunrise or 05:30, the sun for the day, 20:30, 22:00)",
            ),
          ),
          error,
          LM.footer(
            h("button", { onclick: () => close(undefined) }, "Cancel"),
            h("button", { class: "primary confirm", onclick: make }, "Create"),
          ),
        );
      },
      { name: "new-profile" },
    );
    if (!result || !sx) return;
    const start = result.standard
      ? structuredClone(sx.view.standard)
      : structuredClone({
          sections: current.sections,
          collision_policy: current.collision_policy,
          min_section_minutes: current.min_section_minutes,
        });
    const id = newId(result.name);
    sx.draft.push({ id, name: result.name, ...start });
    sx.selected = id;
    pendingFocus = `name:${id}`;
    changed({ full: true });
  }

  function profileEditor(profile) {
    const pid = profile.id;
    const rooms = usedBy(pid);
    const name = h("input", {
      type: "text",
      class: "profile-name-input",
      dataset: { focus: `name:${pid}` },
      value: profile.name || "",
      placeholder: pid,
      "aria-label": "Profile name",
      // A name is optional (the id stands in for it), but never blank. The list shows the
      // new name as it is typed, without a redraw: redrawn on the field's change event, the
      // Save button would be replaced under a click that blurs the field, and the click lost.
      oninput: (event) => {
        const text = event.target.value.trim();
        if (text) profile.name = text;
        else delete profile.name;
        const shown = document.querySelector(`li.profile-item[data-profile="${pid}"] .profile-label`);
        if (shown) shown.textContent = label(profile);
        changed();
      },
    });
    return [
      h(
        "div",
        { class: "profile-fields" },
        h("label", { class: "field" }, h("span", {}, "Name"), name),
        h("div", { class: "field" }, h("span", {}, "Id"), h("code", { class: "profile-id" }, pid)),
      ),
      rooms.length
        ? h(
            "div",
            { class: "box warn shared-warning" },
            h("strong", {}, rooms.length === 1 ? "Used by one room: " : `Shared by ${rooms.length} rooms: `),
            rooms.map((r) => r.name).join(", "),
            ". Saving changes the schedule of ",
            rooms.length === 1 ? "that room." : "every one of them.",
          )
        : h("p", { class: "muted no-rooms" }, "No adaptive room follows this profile yet."),
      policyField(profile),
      minimumField(profile),
      h("h4", {}, "Sections"),
      h(
        "p",
        { class: "muted small" },
        "When each section starts, every day. Sun times come from Home Assistant’s location. When two sections collide (a midsummer sunset after a fixed night time), the higher priority wins; by default a clock time is 100 and a sun time 50.",
      ),
      h(
        "table",
        { class: "sections-table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            ["Section", "Starts", "Priority", "Today"].map((t) => h("th", { scope: "col" }, t)),
          ),
        ),
        h(
          "tbody",
          {},
          profile.sections.map((section) => sectionRow(profile, section)),
        ),
      ),
    ];
  }

  function policyField(profile) {
    return h(
      "fieldset",
      { class: "policy" },
      h("legend", {}, "When two sections collide"),
      POLICIES.map(([value, title, text]) =>
        h(
          "label",
          { class: "policy-choice" },
          h("input", {
            type: "radio",
            name: `policy-${profile.id}`,
            value,
            checked: profile.collision_policy === value,
            onchange: () => {
              profile.collision_policy = value;
              changed();
            },
          }),
          " ",
          h("strong", {}, title),
          `: ${text}`,
        ),
      ),
    );
  }

  function minimumField(profile) {
    const key = `${profile.id}:min`;
    const input = h("input", {
      type: "number",
      class: "knob min-minutes",
      min: "1",
      max: "720",
      step: "1",
      value: String(sx.bad[key] ? sx.bad[key].raw : profile.min_section_minutes),
      "aria-label": "Shortest section, in minutes",
      oninput: (event) => {
        // A number the browser can't parse ("1e") reads as "": invalid, never blank.
        const raw = event.target.value.trim();
        const n = Number(raw);
        const ok = !event.target.validity.badInput && raw !== "" && Number.isInteger(n) && n >= 1 && n <= 720;
        if (ok) {
          profile.min_section_minutes = n;
          delete sx.bad[key];
        } else {
          sx.bad[key] = {
            message: `${label(profile)}: the shortest section must be a whole number of minutes, 1 to 720.`,
            raw: event.target.value,
          };
        }
        event.target.classList.toggle("invalid", !ok);
        changed();
      },
    });
    if (sx.bad[key]) input.classList.add("invalid");
    return h(
      "label",
      { class: "field minimum" },
      h("span", {}, "Shortest section (minutes): a section starting sooner after the one before collides"),
      input,
    );
  }

  function sectionRow(profile, section) {
    const where = `${profile.id}:${section.id}`;
    const prioKey = `${where}:priority`;
    const priority = h("input", {
      type: "number",
      class: "knob priority-input",
      min: "0",
      max: "1000",
      step: "1",
      value: String(sx.bad[prioKey] ? sx.bad[prioKey].raw : (section.priority ?? "")),
      placeholder: "default",
      "aria-label": `${sectionName(section.id)}: priority`,
      dataset: { section: section.id },
      oninput: (event) => {
        const raw = event.target.value.trim();
        const n = Number(raw);
        let ok = true;
        // A number the browser can't parse ("1e") reads as "": invalid, not "the default".
        if (event.target.validity.badInput) ok = false;
        else if (raw === "") delete section.priority;
        else if (Number.isInteger(n) && n >= 0 && n <= 1000) section.priority = n;
        else ok = false;
        if (ok) delete sx.bad[prioKey];
        else {
          sx.bad[prioKey] = {
            message: `${label(profile)}: ${sectionName(section.id)}: a priority is a whole number, 0 to 1000 (empty: the default).`,
            raw: event.target.value,
          };
        }
        event.target.classList.toggle("invalid", !ok);
        changed();
      },
    });
    if (sx.bad[prioKey]) priority.classList.add("invalid");
    return h(
      "tr",
      { class: "section-row", dataset: { section: section.id } },
      h("th", { scope: "row", class: "section-name" }, sectionName(section.id)),
      h(
        "td",
        { class: "trigger-cell", "data-label": "Starts" },
        triggerEditor(profile, section.id, section.trigger, `${where}:t`, 0, (replacement) => {
          section.trigger = replacement;
        }),
      ),
      h("td", { class: "priority-cell", "data-label": "Priority" }, priority),
      h("td", { class: "today-cell", "data-label": "Today", dataset: { section: section.id } }),
    );
  }

  // A trigger's fields, edited in place in the draft. A composite holds two or more clock
  // or sun components (a deeper one, written by hand, is shown and kept as it is).
  function triggerEditor(profile, sectionId, trigger, key, depth, replace) {
    const what = `${label(profile)}: ${sectionName(sectionId)}`;
    const composite = COMPOSITE.includes(trigger.type);
    const types = depth === 0 || composite ? TRIGGER_TYPES : TRIGGER_TYPES.filter(([t]) => !COMPOSITE.includes(t));
    const type = h(
      "select",
      {
        class: "trigger-type",
        "aria-label": `${sectionName(sectionId)}: ${depth ? "component" : "starts"}`,
        dataset: { focus: `${key}:type` },
        // Arrowing through the types fires a change at each: each redraw keeps the focus
        // here, and coming back to a type restores the trigger as it was.
        onchange: (event) => {
          forget(key);
          const shapes = (sx.shapes[key] ||= {});
          shapes[trigger.type] = trigger;
          replace(retype(trigger, event.target.value, shapes));
          changed({ full: true });
        },
      },
      types.map(([value, text]) => h("option", { value }, text)),
    );
    type.value = trigger.type;
    const parts = [type];
    if (trigger.type === "clock") {
      const timeKey = `${key}:time`;
      // Text, not type=time: a browser shows that in its own 12- or 24-hour style, and the
      // schedule is HA's 24-hour wall clock.
      const time = h("input", {
        type: "text",
        inputmode: "numeric",
        maxlength: "5",
        placeholder: "HH:MM",
        class: "trigger-time",
        value: sx.bad[timeKey] ? sx.bad[timeKey].raw : trigger.time,
        "aria-label": `${sectionName(sectionId)}: time`,
        oninput: (event) => {
          const value = event.target.value;
          const ok = TIME.test(value);
          if (ok) {
            trigger.time = value;
            delete sx.bad[timeKey];
          } else {
            sx.bad[timeKey] = { message: `${what}: a time is HH:MM, 00:00 to 23:59.`, raw: value };
          }
          event.target.classList.toggle("invalid", !ok);
          changed();
        },
      });
      if (sx.bad[timeKey]) time.classList.add("invalid");
      parts.push(time);
    } else if (trigger.type === "sun") {
      const event = h(
        "select",
        {
          class: "trigger-event",
          "aria-label": `${sectionName(sectionId)}: sun event`,
          onchange: (e) => {
            trigger.event = e.target.value;
            changed();
          },
        },
        h("option", { value: "sunrise" }, "sunrise"),
        h("option", { value: "sunset" }, "sunset"),
      );
      event.value = trigger.event;
      const offsetKey = `${key}:offset`;
      const offset = h("input", {
        type: "number",
        class: "knob trigger-offset",
        min: "-720",
        max: "720",
        step: "1",
        value: String(sx.bad[offsetKey] ? sx.bad[offsetKey].raw : (trigger.offset_minutes ?? 0)),
        "aria-label": `${sectionName(sectionId)}: offset in minutes (negative: before)`,
        oninput: (e) => {
          const raw = e.target.value.trim();
          const n = Number(raw);
          const ok = !e.target.validity.badInput && raw !== "" && Number.isInteger(n) && n >= -720 && n <= 720;
          if (ok) {
            trigger.offset_minutes = n;
            delete sx.bad[offsetKey];
          } else {
            sx.bad[offsetKey] = {
              message: `${what}: an offset is a whole number of minutes, −720 to 720 (negative: before).`,
              raw: e.target.value,
            };
          }
          e.target.classList.toggle("invalid", !ok);
          changed();
        },
      });
      if (sx.bad[offsetKey]) offset.classList.add("invalid");
      parts.push(
        event,
        h("span", { class: "offset-field" }, offset, h("span", { class: "muted small" }, " min (−: before)")),
      );
    } else if (composite) {
      const of = trigger.of || [];
      parts.push(
        h(
          "ol",
          { class: "components" },
          of.map((sub, i) =>
            h(
              "li",
              { class: "component" },
              triggerEditor(profile, sectionId, sub, `${key}.${i}`, depth + 1, (replacement) => {
                of[i] = replacement;
              }),
              h(
                "button",
                {
                  class: "icon remove-component",
                  dataset: { focus: `${key}.${i}:remove`, focusNext: `${key}:add` },
                  "aria-label": `${sectionName(sectionId)}: remove this time`,
                  title: of.length <= 2 ? "The earlier or later of needs two or more" : "Remove",
                  disabled: of.length <= 2,
                  onclick: () => {
                    forget(key);
                    of.splice(i, 1);
                    changed({ full: true });
                  },
                },
                "×",
              ),
            ),
          ),
        ),
        h(
          "button",
          {
            class: "link add-component",
            dataset: { focus: `${key}:add` },
            onclick: () => {
              of.push({ type: "clock", time: "07:00" });
              changed({ full: true });
            },
          },
          "+ another time",
        ),
      );
    }
    return h("div", { class: `trigger trigger-${trigger.type}`, dataset: { depth: String(depth) } }, parts);
  }

  // A trigger of another type: the one this trigger was before, if it has been that type
  // (`shapes`), else keeping what carries over: a composite's components when switching
  // between earlier and later, the trigger itself as a composite's first component.
  function retype(trigger, type, shapes = {}) {
    if (type === trigger.type) return trigger;
    if (COMPOSITE.includes(type)) {
      if (COMPOSITE.includes(trigger.type)) return { type, of: trigger.of };
      const before = shapes.earliest || shapes.latest;
      if (before) return { type, of: structuredClone(before.of) };
      return { type, of: [trigger, { type: "clock", time: "07:00" }] };
    }
    if (shapes[type]) return structuredClone(shapes[type]);
    if (type === "clock") return { type, time: "07:00" };
    return { type, event: "sunrise", offset_minutes: 0 };
  }

  // Today's column and the location line, from the last check (or the read): redrawn
  // without touching the fields, so a field being typed in keeps its focus.
  function refreshDays() {
    const line = document.getElementById("schedule-location");
    if (!line || !sx) return;
    const { location } = sx;
    const profile = selected();
    // An own entry only: a profile named "constructor" must not read Object's.
    const day = Object.hasOwn(sx.days, profile.id) ? sx.days[profile.id] : null;
    if (!location.available) {
      line.className = "box warn schedule-location";
      line.textContent = `Today’s times can’t be shown: ${location.error}.`;
    } else {
      line.className = "muted small schedule-location";
      line.textContent = day
        ? `Today (${day.date}) at Home Assistant’s location, in ${location.time_zone}.`
        : `Today at Home Assistant’s location, in ${location.time_zone}.`;
    }
    for (const cell of document.querySelectorAll(".profile-card td.today-cell")) {
      const boundary = day?.boundaries.find((b) => b.section === cell.dataset.section);
      fill(cell, todayCell(boundary, Boolean(location.available && day)));
      const input = cell.parentElement.querySelector(".priority-input");
      // The trigger's default, as the scheduler gives it (a clock 100, a sun time 50, a
      // composite its firmest component's), where the field is left empty.
      if (input) input.placeholder = boundary?.default_priority ? `default (${boundary.priority})` : "default";
    }
  }

  function todayCell(boundary, known) {
    if (!known || !boundary) return h("span", { class: "muted" }, "—");
    if (boundary.outcome === "collapsed") {
      return [
        h("span", { class: "badge skipped" }, "SKIPPED"),
        boundary.start ? h("span", { class: "muted" }, ` (${boundary.start})`) : null,
        h("span", { class: "muted small block" }, boundary.reason),
      ];
    }
    if (boundary.outcome === "clamped") {
      return [
        h("span", { class: "today-time" }, boundary.start),
        " ",
        h("span", { class: "badge moved" }, "MOVED"),
        h("span", { class: "muted small block" }, boundary.reason),
      ];
    }
    return h("span", { class: "today-time" }, boundary.start);
  }

  // -- Saving --------------------------------------------------------------------------

  async function save() {
    if (!sx || sx.saving || !dirty() || problems().length) return;
    const mine = sx;
    mine.saving = true;
    refreshBar();
    // Inert while the save runs and the editor reloads after it: an edit made meanwhile
    // would be dropped by the reload without a word.
    const el = host();
    if (el) el.inert = true;
    let result;
    try {
      result = await api("PUT", "/api/schedule_profiles", { schedule_profiles: mine.draft, base: mine.view.version });
    } catch (err) {
      mine.saving = false;
      if (el && sx === mine) el.inert = false;
      if (sx !== mine) throw err;
      if (err.error === "edit_stale") {
        // Changed since the editor showed it (another tab, or a room given another
        // profile). Expected, so not fail(): say so and show it as it is now; saving
        // this draft would undo the other change.
        notify(err.message, { kind: "error" });
        await load(mine.selected);
        return;
      }
      if (err.problems.length) {
        mine.serverProblems = err.problems;
        refreshBar();
        return;
      }
      refreshBar();
      throw err;
    }
    const changes = result.changes.length;
    notify(
      `Saved the schedule profiles${changes ? ` (${plural(changes, "change")})` : ""}. Nothing is deployed: the rooms follow their profiles once adaptive runs, at cutover (P6).`,
      { kind: "ok", timeout: 8000 },
    );
    if (sx === mine) await load(mine.selected);
  }

  async function revert() {
    if (!sx || !edited()) return;
    const sure = await confirmBox("Revert?", ["Drop the unsaved changes and show the schedule profiles as saved."], {
      confirm: "Revert",
      danger: true,
    });
    if (sure && sx) await load(sx.selected);
  }
})();
