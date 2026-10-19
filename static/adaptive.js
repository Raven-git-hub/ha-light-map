// The adaptive views (P5.2b): the Now, Analysis and Almanac tabs, read-only, one adaptive
// room at a time (#now/<room>, #analysis/<room>, #almanac/<room>), with the adaptive rooms to
// pick from at the top; the room picked is kept while moving between the three.
//
// What they read, and what they never read (docs/DESIGN.md, "The adaptive views"): the
// schedule comes from the config and HA's location, the learned data from Light Map's own
// store. A live al_ entity (the scene select, the guard, the hold) belongs to the old
// ha-adaptive-light until cutover (P6), so the server reads one only with the cutover switch
// on; off, the Now tab says adaptive isn't live yet in its place. The Almanac tab shows the
// store's almanac, never HA's sensor.al_almanac_. Nothing here writes anything.
//
// Read from /api (see app/main.py); every name is set as text, never as markup. The
// Analysis charts are uPlot (static/vendor/uplot, loaded when a chart is first drawn, so
// no other view parses it), one measure per chart (never two
// y-axes), coloured from the theme's tokens, each series told apart by more than colour:
// its dash, its line in the legend, a label at its end, and the table under the charts.

"use strict";

(() => {
  const { h, fill, api, fail, ApiError, SECTIONS, sectionName, describeTrigger, plural } = LM;

  const TABS = { now: "Now", analysis: "Analysis", almanac: "Almanac" };
  // The sections' labels where the chart is narrow (a phone).
  const SHORT_NAMES = { sunrise: "Rise", day: "Day", afternoon: "Aft.", sunset: "Set", night: "Night", sleep: "Sleep" };
  // A unit's line, by its slot: the cue beside colour, on the chart and in its legend.
  const DASHES = [[], [10, 5], [2, 4], [12, 4, 2, 4], [6, 6], [2, 8], [14, 6, 6, 6], [4, 3]];
  // One per categorical colour slot (--series-1..8), assigned in order and never cycled: a
  // ninth unit is left to the table rather than given a colour another unit already has.
  const MAX_SERIES = 8;
  // The errors that are an answer, not a failure: shown in the view, not as a notice.
  const EXPECTED = ["not_adaptive", "not_found", "store_unavailable"];

  const CHART_LIBRARY = "vendor/uplot/uPlot.iife.min.js";
  let chartLibrary = null; // the load, once asked for

  let lastRoom = null;
  // The charts on screen: {plot, wrap, height}. Destroyed when the view goes.
  let charts = [];

  for (const tab of Object.keys(TABS)) LM.view(tab, (main, roomId) => show(tab, main, roomId));

  window.addEventListener("resize", () => {
    fitLabels();
    pruneCharts();
    for (const chart of charts) {
      // A zoom changes the pixel ratio too: the dashes follow it (see sectionChart).
      if (chart.ratio !== devicePixelRatio) {
        chart.ratio = devicePixelRatio;
        chart.plot.series.slice(1).forEach((s, i) => {
          s.dash = scaled(chart.dashes[i]);
        });
      }
      chart.plot.setSize({ width: chartWidth(chart.wrap), height: chart.height });
    }
  });
  // Leaving for another view drops its charts (they are detached once the router has
  // replaced the view): uPlot keeps every live chart in its own scroll and resize lists.
  window.addEventListener("hashchange", () => setTimeout(pruneCharts));

  async function show(tab, main, roomId) {
    destroyCharts();
    const host = h(
      "section",
      { id: "adaptive-view", class: `adaptive-view ${tab}-view`, dataset: { tab } },
      h("p", { class: "muted" }, "Loading…"),
    );
    main.append(host);
    const map = await api("GET", "/api/map");
    if (!host.isConnected) return; // another view is showing now
    const rooms = map.rooms.filter((r) => r.adaptive);
    if (!rooms.length) {
      fill(host, h("h2", {}, TABS[tab]), noRooms());
      return;
    }
    const room = rooms.find((r) => r.id === roomId) || rooms.find((r) => r.id === lastRoom) || rooms[0];
    if (roomId && roomId !== room.id) {
      LM.notify(`There is no adaptive room “${roomId}”; showing ${room.name}.`, { timeout: 8000 });
    }
    lastRoom = room.id;
    LM.replaceRoute(`#${tab}/${encodeURIComponent(room.id)}`);
    host.dataset.room = room.id;
    const body = h("div", { class: "adaptive-body" }, h("p", { class: "muted" }, "Loading…"));
    fill(host, picker(tab, rooms, room), body);
    if (tab === "now") await renderNow(body, room);
    else if (tab === "analysis") await renderAnalysis(body, room);
    else await renderAlmanac(body, room);
  }

  function picker(tab, rooms, room) {
    return h(
      "nav",
      { class: "room-picker", "aria-label": "Adaptive rooms" },
      rooms.map((r) =>
        h(
          "a",
          {
            class: "room-pill",
            href: `#${tab}/${encodeURIComponent(r.id)}`,
            "aria-current": r.id === room.id ? "page" : null,
            dataset: { room: r.id },
          },
          r.name,
        ),
      ),
    );
  }

  function noRooms() {
    return h(
      "div",
      { class: "box no-adaptive-rooms" },
      "No room is adaptive yet. Tick ",
      h("strong", {}, "Adaptive"),
      " beside a room on the ",
      h("a", { href: "#map" }, "Map"),
      ", then choose its adaptive bulbs in its ",
      h("a", { href: "#config" }, "config"),
      ".",
    );
  }

  // A view's read failed: say why where the view would be. An expected answer (the room
  // isn't adaptive any more, the store didn't open) isn't also raised as a notice.
  function unavailable(body, err) {
    if (!body.isConnected) return; // the view was left: its failure isn't the next view's
    fill(body, h("div", { class: "box error view-unavailable" }, err.message));
    if (!(err instanceof ApiError) || !EXPECTED.includes(err.error)) fail(err);
  }

  function head(title, ...extra) {
    return h("header", { class: "view-head" }, h("h2", {}, title), ...extra);
  }

  function card(name, title, ...body) {
    return h("section", { class: `editor-card ${name}-card` }, h("h3", {}, title), ...body);
  }

  function refreshButton(label, onclick) {
    return h("button", { class: "refresh", onclick: () => onclick().catch(fail) }, label);
  }

  // -- Values --------------------------------------------------------------------------

  const pct = (brightness) => Math.round((brightness / 255) * 100);

  // A unit's effective target, as the almanac publishes it: a brightness, "off" (held off
  // for the section), or nothing learned yet.
  function targetText(target) {
    if (target === "off") return [h("span", { class: "badge off" }, "OFF"), " held off"];
    if (typeof target === "number") return h("span", { class: "target" }, `${target} · ${pct(target)}%`);
    return h("span", { class: "muted none" }, "nothing learned yet");
  }

  function luxText(lux, source) {
    if (lux === null || lux === undefined) return h("span", { class: "muted" }, "none yet (observing)");
    return [`${lux} lx `, h("span", { class: "muted small" }, source === "seed" ? "(seed)" : "(learned)")];
  }

  function swatch(section) {
    const slot = SECTIONS.indexOf(section) + 1;
    return h("span", { class: "section-key", "aria-hidden": "true", style: `--key: var(--series-${slot})` });
  }

  function minutes(hhmm) {
    const [hh, mm] = hhmm.split(":").map(Number);
    return hh * 60 + mm;
  }

  function longDate(iso) {
    const date = new Date(`${iso}T12:00:00`);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  }

  // -- Now ------------------------------------------------------------------------------

  async function renderNow(body, room) {
    let view;
    try {
      view = await api("GET", `/api/rooms/${encodeURIComponent(room.id)}/now`);
    } catch (err) {
      unavailable(body, err);
      return;
    }
    if (!body.isConnected) return;
    const { schedule } = view;
    fill(
      body,
      head(
        `Now · ${view.room.name}`,
        schedule.available
          ? h("span", { class: "muted when" }, `${longDate(schedule.date)} · ${schedule.now} (${schedule.time_zone})`)
          : null,
        h("span", { class: "spacer" }),
        refreshButton("Refresh", () => renderNow(body, room)),
      ),
      card("live", "Live state", liveSection(view.live)),
      card("schedule", "Today’s schedule", scheduleSection(view)),
      card("targets", "Targets now", targetsSection(view)),
    );
    fitLabels();
  }

  // A segment's label only where it fits whole: never clipped by the next segment. The
  // segment's title and the table carry it otherwise.
  function fitLabels() {
    for (const label of document.querySelectorAll(".day-bar .segment .label")) {
      label.hidden = false;
      label.hidden = label.offsetWidth > label.parentElement.clientWidth - 6;
    }
  }

  function liveSection(live) {
    if (!live.wired) {
      return h(
        "div",
        { class: "box warn not-live", role: "status" },
        h("p", {}, h("strong", {}, live.title)),
        h("p", {}, live.note),
      );
    }
    const rows = [];
    if (live.runtime) {
      rows.push([
        "Section",
        live.runtime.section ? sectionName(live.runtime.section) : "not yet fired",
        live.runtime.running ? "the runtime is running" : "the runtime is stopped",
      ]);
    } else {
      rows.push(["Runtime", "not running", "nothing fires a scene until it runs"]);
    }
    if (live.available) {
      const { scene, guard, hold } = live.states;
      rows.push(["Scene select", scene ? sectionName(scene) : "missing", live.entities.scene]);
      rows.push([
        "Guard",
        guard === "on" ? "raised" : guard === "off" ? "lowered" : guard || "missing",
        guard === "on" ? "a scene or maintenance is changing the lights" : live.entities.guard,
      ]);
      rows.push([
        "Hold",
        hold === "on" ? "on" : hold === "off" ? "off" : hold || "missing",
        hold === "on" ? "maintenance stands down for the rest of the section" : live.entities.hold,
      ]);
    }
    return [
      live.available ? null : h("div", { class: "box error live-unavailable" }, live.error),
      h(
        "dl",
        { class: "live-state" },
        rows.map(([term, value, note]) => [
          h("dt", {}, term),
          h(
            "dd",
            { dataset: { live: term.toLowerCase().replace(/ /g, "-") } },
            value,
            " ",
            h("span", { class: "muted small" }, note),
          ),
        ]),
      ),
    ];
  }

  function scheduleSection(view) {
    const { schedule, profile } = view;
    const shared = profile.shared_with.map((r) => r.name);
    const nodes = [
      h(
        "p",
        { class: "profile-line" },
        "Follows the profile ",
        h("strong", { class: "profile-name" }, profile.name),
        profile.name !== profile.id ? [" ", h("code", { class: "muted" }, profile.id)] : null,
        shared.length ? `, shared with ${shared.join(", ")}` : null,
        ". ",
        h("a", { class: "edit-schedule", href: `#schedule/${encodeURIComponent(profile.id)}` }, "Edit the schedule"),
      ),
    ];
    if (!schedule.available) {
      nodes.push(h("div", { class: "box error schedule-unavailable" }, schedule.error));
      return nodes;
    }
    nodes.push(dayBar(schedule), scheduleTable(schedule));
    const { current } = schedule;
    if (current?.carried_over) {
      nodes.push(
        h(
          "p",
          { class: "muted small carried-over" },
          `${sectionName(current.section)} has been in force since ${current.since} yesterday: today’s first section hasn’t started yet.`,
        ),
      );
    }
    if (!current) nodes.push(h("p", { class: "muted small" }, "No section is in force: none has started in two days."));
    return nodes;
  }

  // The day as one bar, 00:00 to 24:00: a segment per section that runs today, the section
  // in force outlined and labelled, and a marker at the time now. The table below has
  // everything the bar shows, for a reader who can't see it.
  function dayBar(schedule) {
    const ran = schedule.boundaries.filter((b) => b.outcome !== "collapsed");
    const now = minutes(schedule.now);
    const segments = [];
    if (ran.length && minutes(ran[0].start) > 0) {
      // Before the day's first boundary: yesterday's last section, known exactly only while
      // it is the one in force.
      const carried = schedule.current?.carried_over ? schedule.current.section : null;
      segments.push({ section: carried, from: 0, to: minutes(ran[0].start), current: Boolean(carried), before: true });
    }
    for (const b of ran) {
      segments.push({
        section: b.section,
        from: minutes(b.start),
        to: b.ends ? minutes(b.ends) : 1440,
        current: b.current,
      });
    }
    const label = ran.map((b) => `${sectionName(b.section)} from ${b.start}`).join(", ");
    return h(
      "div",
      { class: "day-bar-wrap" },
      h(
        "div",
        {
          class: "day-bar",
          role: "img",
          "aria-label": `Today: ${label || "no section starts"}. It is ${schedule.now}.`,
        },
        segments.map((s) => {
          // Never negative: on a day the clocks go back, a section can start in the repeated
          // hour at a time that reads earlier than the one before it (the table says so).
          const width = (Math.max(0, s.to - s.from) / 1440) * 100;
          const slot = s.section ? SECTIONS.indexOf(s.section) + 1 : 0;
          return h(
            "div",
            {
              class: `segment${s.current ? " current" : ""}${s.before ? " before" : ""}`,
              style: `left: ${(s.from / 1440) * 100}%; width: ${width}%;${slot ? ` --key: var(--series-${slot});` : ""}`,
              title: s.section
                ? `${sectionName(s.section)}${s.before ? " (from yesterday)" : ""}`
                : "Yesterday’s last section",
              dataset: { section: s.section || "" },
            },
            // Labelled where it fits (fitLabels); the title and the table carry the rest.
            s.section ? h("span", { class: "label" }, sectionName(s.section)) : null,
          );
        }),
        h("div", { class: "now-marker", style: `left: ${(now / 1440) * 100}%` }),
      ),
      h(
        "div",
        { class: "day-ticks", "aria-hidden": "true" },
        ["00:00", "06:00", "12:00", "18:00", "24:00"].map((t, i) => h("span", { style: `left: ${i * 25}%` }, t)),
      ),
    );
  }

  function scheduleTable(schedule) {
    return h(
      "table",
      { class: "schedule-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          ["Section", "Starts", "Ends", "Trigger", "Priority", "Today"].map((t) => h("th", { scope: "col" }, t)),
        ),
      ),
      h(
        "tbody",
        {},
        schedule.boundaries.map((b) => {
          const collapsed = b.outcome === "collapsed";
          return h(
            "tr",
            {
              class: `boundary ${b.outcome}${b.current ? " current" : ""}`,
              dataset: { section: b.section },
              "aria-current": b.current ? "time" : null,
            },
            h(
              "th",
              { scope: "row", class: "section-cell" },
              swatch(b.section),
              sectionName(b.section),
              b.current ? [" ", h("span", { class: "badge now" }, "NOW")] : null,
            ),
            h(
              "td",
              { "data-label": "Starts", class: "start" },
              collapsed ? (b.start ? h("span", { class: "muted" }, `(${b.start})`) : "—") : b.start,
            ),
            h("td", { "data-label": "Ends" }, collapsed ? "—" : b.ends || "into tomorrow"),
            h("td", { "data-label": "Trigger" }, describeTrigger(b.trigger)),
            h("td", { "data-label": "Priority" }, `${b.priority}${b.default_priority ? " (default)" : ""}`),
            h(
              "td",
              { "data-label": "Today", class: "outcome" },
              collapsed
                ? [h("span", { class: "badge skipped" }, "SKIPPED"), " ", b.reason]
                : b.outcome === "clamped"
                  ? [h("span", { class: "badge moved" }, "MOVED"), " ", b.reason]
                  : "runs",
            ),
          );
        }),
      ),
    );
  }

  function targetsSection(view) {
    const t = view.targets;
    if (!t.available) return h("p", { class: "muted targets-unavailable" }, t.error);
    return [
      h(
        "p",
        { class: "muted small" },
        `What Light Map’s almanac holds for ${sectionName(t.section)}, the section in force: each learning unit’s target as it would be published. `,
        t.valid_from
          ? `The snapshot in force took effect on ${t.valid_from}.`
          : "Nothing has been learned for this room yet.",
        view.live.wired ? "" : " Nothing is applied until cutover: ha-adaptive-light still drives the room.",
      ),
      t.units.length
        ? h(
            "table",
            { class: "targets-table" },
            h(
              "thead",
              {},
              h(
                "tr",
                {},
                ["Learning unit", "Bulbs", "Mode", "Target"].map((x) => h("th", { scope: "col" }, x)),
              ),
            ),
            h(
              "tbody",
              {},
              t.units.map((u) =>
                h(
                  "tr",
                  { dataset: { unit: u.id } },
                  h("th", { scope: "row" }, u.name),
                  h("td", { "data-label": "Bulbs", class: "muted" }, u.members.map((m) => m.name).join(", ")),
                  h("td", { "data-label": "Mode" }, u.mode === "off" ? "Off" : "Auto"),
                  h("td", { "data-label": "Target", class: "target-cell" }, targetText(u.target)),
                ),
              ),
            ),
          )
        : h("p", { class: "muted none" }, "No learning units yet: every bulb in this room is switched-only."),
      h(
        "p",
        { class: "lux-line" },
        "Lux target: ",
        luxText(t.lux_target, t.lux_source),
        ` · maintenance ${t.maintenance_enabled ? "on" : "off"} in this section`,
      ),
    ];
  }

  // -- Analysis -------------------------------------------------------------------------

  async function renderAnalysis(body, room) {
    let view;
    try {
      view = await api("GET", `/api/rooms/${encodeURIComponent(room.id)}/analysis`);
    } catch (err) {
      unavailable(body, err);
      return;
    }
    if (!body.isConnected) return;
    destroyCharts();
    const learned = view.sections.some((s) => s.learned);
    const brightnessWrap = h("div", { class: "chart", tabindex: "0" });
    const luxWrap = h("div", { class: "chart", tabindex: "0" });
    const hasLux = view.sections.some((s) => s.learned && s.lux_target !== null);
    fill(
      body,
      head(
        `Analysis · ${view.room.name}`,
        h("span", { class: "spacer" }),
        refreshButton("Run again", () => renderAnalysis(body, room)),
      ),
      h(
        "p",
        { class: "muted intro" },
        `Run now over Light Map’s store, as the nightly pass would: the last ${plural(view.learning.lookback_days, "day")} of observations, each day counted in ${view.clock.time_zone}. Computing it records nothing.`,
      ),
      view.clock.note ? h("div", { class: "box warn clock-note" }, view.clock.note) : null,
      card("state", "Learning state", stateSection(view)),
      learned
        ? card(
            "brightness",
            "Learned brightness by section",
            h(
              "p",
              { class: "muted small" },
              "Each learning unit’s learned brightness (0–255, as Home Assistant counts it) for each section of the day. A gap is a section with nothing learned for that unit.",
            ),
            h("div", { class: "chart-legend-row" }),
            brightnessWrap,
            h("p", { class: "chart-readout muted small", "aria-live": "polite" }),
          )
        : null,
      learned
        ? card(
            "lux",
            "Lux target by section",
            hasLux
              ? [h("p", { class: "muted small" }, "The room’s learned lux target for each section, in lux."), luxWrap]
              : h("p", { class: "muted none" }, "No lux target learned yet."),
            hasLux ? h("p", { class: "chart-readout muted small", "aria-live": "polite" }) : null,
          )
        : null,
      learned || view.units.length ? card("table", "By section", analysisTable(view)) : null,
    );
    if (!learned) return;
    await loadChartLibrary();
    if (!body.isConnected) return;
    const units = view.units.slice(0, MAX_SERIES);
    const brightness = {
      series: units.map((u, i) => ({
        name: u.name,
        color: `--series-${i + 1}`,
        dash: DASHES[i],
        values: view.sections.map((s) => (s.learned ? s.units[u.id].brightness : null)),
      })),
      range: [0, 255],
      splits: [0, 64, 128, 192, 255],
      label: "Brightness (0–255)",
      format: (v) => `${v} (${pct(v)}%)`,
      height: 280,
      endLabels: true,
    };
    const legendRow = body.querySelector(".brightness-card .chart-legend-row");
    fill(
      legendRow,
      legend(brightness.series),
      units.length < view.units.length ? overflowNote(view.units.length) : null,
    );
    sectionChart(
      brightnessWrap,
      body.querySelector(".brightness-card .chart-readout"),
      brightness,
      "Learned brightness",
    );
    if (hasLux) {
      const values = view.sections.map((s) => (s.learned ? s.lux_target : null));
      const top = Math.max(...values.filter((v) => v !== null));
      sectionChart(
        luxWrap,
        body.querySelector(".lux-card .chart-readout"),
        {
          series: [{ name: "Lux target", color: "--chart-ink", dash: [], values }],
          range: [0, top > 0 ? Math.ceil((top * 1.15) / 10) * 10 : 10],
          label: "Lux",
          format: (v) => `${v} lx`,
          height: 180,
          endLabels: false,
        },
        "Lux target",
      );
    }
  }

  function overflowNote(count) {
    return h(
      "p",
      { class: "muted small" },
      `The chart shows the first ${MAX_SERIES} of ${count} learning units (one colour each, never repeated); the table has them all.`,
    );
  }

  function stateSection(view) {
    const { state, learning } = view;
    if (state.mode === "no_units") {
      return h(
        "p",
        { class: "state-line" },
        "No learning units yet, so there is nothing to analyse. Make bulbs adaptive in ",
        h("a", { href: `#config/${encodeURIComponent(view.room.id)}` }, "this room’s config"),
        ".",
      );
    }
    if (state.mode === "empty") {
      return h(
        "p",
        { class: "state-line" },
        h("span", { class: "pill" }, "Nothing yet"),
        ` No observation in the last ${plural(learning.lookback_days, "day")} is filed under a section, so there is nothing to learn from.`,
        view.adaptive_live
          ? ""
          : " Observations are filed under a section once the adaptive runtime runs, from cutover (P6).",
      );
    }
    if (state.mode === "bootstrap") {
      return [
        h(
          "p",
          { class: "state-line" },
          h("span", { class: "pill warn mode" }, "Bootstrap"),
          ` ${state.days_analysed} of the ${plural(learning.bootstrap_min_days, "day")} it takes to start learning. A snapshot from this pass would take effect on ${state.valid_from}: while bootstrapping, a pass takes effect at once.`,
        ),
        h("progress", {
          class: "bootstrap-progress",
          max: String(learning.bootstrap_min_days),
          value: String(state.days_analysed),
          "aria-label": `${state.days_analysed} of ${learning.bootstrap_min_days} bootstrap days`,
        }),
      ];
    }
    return h(
      "p",
      { class: "state-line" },
      h("span", { class: "pill ok mode" }, "Learning"),
      ` ${plural(state.days_analysed, "day")} analysed, the newest ${state.updated}. A snapshot from this pass would take effect on ${state.valid_from}, ${plural(learning.publish_delay_days, "day")} after its newest day, so a big swing can’t land overnight.`,
    );
  }

  function analysisTable(view) {
    const units = view.units;
    return h(
      "div",
      { class: "matrix-wrap" },
      h(
        "table",
        { class: "analysis-table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", { scope: "col", class: "section-head" }, "Section"),
            units.map((u) => h("th", { scope: "col", dataset: { unit: u.id } }, u.name)),
            ["Lux target", "Days", "High-confidence days", "Trust weight"].map((t) => h("th", { scope: "col" }, t)),
          ),
        ),
        h(
          "tbody",
          {},
          view.sections.map((s) =>
            h(
              "tr",
              { dataset: { section: s.section }, class: s.learned ? "learned" : "unlearned" },
              h("th", { scope: "row", class: "section-name" }, sectionName(s.section)),
              s.learned
                ? [
                    units.map((u) => {
                      const cell = s.units[u.id];
                      return h(
                        "td",
                        { dataset: { unit: u.id } },
                        cell.brightness === null
                          ? h("span", { class: "muted" }, "—")
                          : h("span", { class: "target" }, `${cell.brightness} · ${pct(cell.brightness)}%`),
                        cell.on_fraction === null
                          ? null
                          : h(
                              "span",
                              { class: "muted small block" },
                              `on ${Math.round(cell.on_fraction * 100)}% of it`,
                            ),
                        cell.mode === "off"
                          ? h("span", { class: "muted small block" }, "held off: kept for Auto")
                          : null,
                      );
                    }),
                    h("td", {}, luxText(s.lux_target, "learned")),
                    h("td", {}, String(s.days_contributing)),
                    h("td", {}, String(s.high_confidence_days)),
                    h("td", {}, String(s.trust_weight)),
                  ]
                : h("td", { colspan: String(units.length + 4), class: "muted" }, "Nothing learned"),
            ),
          ),
        ),
      ),
    );
  }

  // -- The chart ------------------------------------------------------------------------

  // The chart library, from the app itself (vendored), the first time a chart is drawn. A
  // failure leaves `uPlot` undefined, and each chart says so in its place: the table under
  // the charts has every value either way.
  function loadChartLibrary() {
    if (typeof uPlot === "function") return Promise.resolve();
    chartLibrary ||= new Promise((resolve) => {
      const script = h("script", { src: CHART_LIBRARY });
      const done = () => {
        if (typeof uPlot !== "function") {
          // Not cached as loaded: the next drawing tries again.
          chartLibrary = null;
          script.remove();
        }
        resolve();
      };
      script.addEventListener("load", done);
      script.addEventListener("error", done);
      document.head.append(script);
    });
    return chartLibrary;
  }

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // uPlot scales a line's width by the pixel ratio but hands its dash pattern to the canvas
  // as given, so on a HiDPI screen a [2, 4] pattern drew at half size (sub-pixel dots) and
  // stopped matching the legend's key. Scaled here, the dashes are drawn in CSS pixels.
  function scaled(dash) {
    return dash.map((d) => d * (devicePixelRatio || 1));
  }

  function chartWidth(wrap) {
    return Math.max(wrap.clientWidth, 260);
  }

  const SVG = "http://www.w3.org/2000/svg";

  // A series' key as it is drawn: its line, dash and marker.
  function lineKey(color, dash) {
    const svg = document.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({ width: "34", height: "12", viewBox: "0 0 34 12", "aria-hidden": "true" })) {
      svg.setAttribute(k, v);
    }
    svg.classList.add("line-key");
    const line = document.createElementNS(SVG, "line");
    for (const [k, v] of Object.entries({ x1: "1", y1: "6", x2: "33", y2: "6", stroke: color, "stroke-width": "2" })) {
      line.setAttribute(k, v);
    }
    line.setAttribute("stroke-linecap", "round");
    if (dash.length) line.setAttribute("stroke-dasharray", dash.join(" "));
    const dot = document.createElementNS(SVG, "circle");
    for (const [k, v] of Object.entries({
      cx: "17",
      cy: "6",
      r: "4",
      fill: color,
      stroke: token("--panel"),
      "stroke-width": "2",
    })) {
      dot.setAttribute(k, v);
    }
    svg.append(line, dot);
    return svg;
  }

  function legend(series) {
    return h(
      "ul",
      { class: "chart-legend" },
      series.map((s) => h("li", {}, lineKey(token(s.color), s.dash), h("span", {}, s.name))),
    );
  }

  // One measure across the six sections (canonical order, which is the order of the day),
  // one line per series. The pointer, or the arrow keys on the focused chart, picks a
  // section, and the readout under it lists every series there.
  function sectionChart(wrap, readout, spec, title) {
    if (typeof uPlot !== "function") {
      wrap.removeAttribute("tabindex");
      fill(wrap, h("p", { class: "muted" }, "The chart didn’t load; the table below has every value."));
      return;
    }
    const xs = SECTIONS.map((_, i) => i);
    const ink = token("--muted");
    const grid = token("--chart-grid");
    const surface = token("--panel");
    const font = "12px system-ui, -apple-system, 'Segoe UI', sans-serif";
    const series = spec.series.map((s) => ({ ...s, stroke: token(s.color) }));
    // A group, not an image: it takes the keyboard (the arrows walk the sections).
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-roledescription", "chart");
    wrap.setAttribute(
      "aria-label",
      `${title} by section${series.length > 1 ? ` for ${series.map((s) => s.name).join(", ")}` : ""}. Use the arrow keys for each section’s values; the table below has them all.`,
    );
    const hint = "Point at the chart, or focus it and use the arrow keys, for a section’s values.";
    // Refilled only when the section changes: the readout is a live region, and every
    // pointer move inside one section would otherwise announce it again.
    let shown;
    const showReadout = (idx) => {
      if (!readout || idx === shown) return;
      shown = idx;
      if (idx === null || idx === undefined) {
        fill(readout, hint);
        return;
      }
      fill(
        readout,
        h("strong", {}, `${sectionName(SECTIONS[idx])}: `),
        series.map((s, i) => {
          const v = s.values[idx];
          return h(
            "span",
            { class: "readout-item" },
            lineKey(s.stroke, s.dash),
            h("strong", {}, v === null ? "—" : spec.format(v)),
            series.length > 1 ? ` ${s.name}` : "",
            i < series.length - 1 ? " · " : "",
          );
        }),
      );
    };
    showReadout(null);
    const opts = {
      width: chartWidth(wrap),
      height: spec.height,
      padding: [18, 18, 0, 4],
      scales: { x: { time: false, range: [-0.35, 5.35] }, y: { range: spec.range } },
      axes: [
        {
          stroke: ink,
          font,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid, width: 1 },
          splits: () => xs,
          values: (u, splits) =>
            splits.map((i) => (wrap.clientWidth < 520 ? SHORT_NAMES : {})[SECTIONS[i]] || sectionName(SECTIONS[i])),
        },
        {
          stroke: ink,
          font,
          labelFont: font,
          label: spec.label,
          size: 52,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid, width: 1 },
          ...(spec.splits ? { splits: () => spec.splits } : {}),
        },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.name,
          stroke: s.stroke,
          width: 2,
          dash: scaled(s.dash),
          spanGaps: false,
          points: { show: true, size: 10, width: 2, stroke: surface, fill: s.stroke },
        })),
      ],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 12 } },
      hooks: {
        setCursor: [(u) => showReadout(u.cursor.idx)],
        draw: spec.endLabels ? [endLabels(series)] : [],
      },
    };
    const plot = new uPlot(opts, [xs, ...series.map((s) => s.values)], wrap);
    charts.push({ plot, wrap, height: spec.height, dashes: series.map((s) => s.dash), ratio: devicePixelRatio });
    let focused = null;
    wrap.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      focused = focused === null ? 0 : Math.min(5, Math.max(0, focused + (event.key === "ArrowRight" ? 1 : -1)));
      plot.setCursor({ left: plot.valToPos(focused, "x"), top: plot.bbox.height / devicePixelRatio / 2 });
    });
    wrap.addEventListener("blur", () => {
      focused = null;
    });
  }

  // A label at each line's last point, for up to four series, in the text colour (never
  // the series colour). One that would collide with another is left out: the legend, the
  // readout and the table still name it.
  function endLabels(series) {
    return (u) => {
      // On a narrow chart a label would sit on the lines; the legend names them there.
      if (series.length < 2 || series.length > 4 || u.over.clientWidth < 420) return;
      const ratio = devicePixelRatio || 1;
      const ctx = u.ctx;
      ctx.save();
      ctx.font = `${12 * ratio}px system-ui, -apple-system, 'Segoe UI', sans-serif`;
      ctx.fillStyle = token("--fg");
      ctx.textBaseline = "bottom";
      const placed = [];
      series.forEach((s) => {
        let last = -1;
        for (let i = s.values.length - 1; i >= 0; i -= 1) {
          if (s.values[i] !== null) {
            last = i;
            break;
          }
        }
        if (last < 0) return;
        placed.push({ text: s.name, x: u.valToPos(last, "x", true), y: u.valToPos(s.values[last], "y", true) });
      });
      placed.sort((a, b) => a.y - b.y);
      let previous = -Infinity;
      for (const p of placed) {
        if (p.y - previous < 15 * ratio) continue;
        previous = p.y;
        const text = p.text.length > 20 ? `${p.text.slice(0, 19)}…` : p.text;
        const right = p.x > u.bbox.left + u.bbox.width - 120 * ratio;
        ctx.textAlign = right ? "right" : "left";
        ctx.fillText(text, p.x + (right ? -8 : 8) * ratio, p.y - 6 * ratio);
      }
      ctx.restore();
    };
  }

  function pruneCharts() {
    charts = charts.filter((chart) => {
      if (chart.wrap.isConnected) return true;
      chart.plot.destroy();
      return false;
    });
  }

  function destroyCharts() {
    for (const chart of charts) chart.plot.destroy();
    charts = [];
  }

  // -- Almanac --------------------------------------------------------------------------

  async function renderAlmanac(body, room) {
    let view;
    try {
      view = await api("GET", `/api/rooms/${encodeURIComponent(room.id)}/almanac`);
    } catch (err) {
      unavailable(body, err);
      return;
    }
    if (!body.isConnected) return;
    fill(
      body,
      head(
        `Almanac · ${view.room.name}`,
        h("span", { class: "spacer" }),
        refreshButton("Refresh", () => renderAlmanac(body, room)),
      ),
      h("p", { class: "muted intro almanac-source" }, view.note),
      view.clock.note ? h("div", { class: "box warn clock-note" }, view.clock.note) : null,
      card("snapshot", "In force", snapshotSection(view)),
      card("almanac", "By section", almanacTable(view)),
      view.retired_units.length
        ? h(
            "p",
            { class: "muted small retired" },
            `The store also keeps the history of ${plural(view.retired_units.length, "learning unit")} this room no longer has (${view.retired_units.join(", ")}); history is never deleted.`,
          )
        : null,
    );
  }

  function snapshotSection(view) {
    const s = view.snapshot;
    if (!s) {
      return h(
        "p",
        { class: "state-line no-snapshot" },
        h("span", { class: "pill" }, "Nothing learned yet"),
        " Light Map’s store holds no almanac snapshot for this room.",
        view.adaptive_live
          ? " The nightly pass records one once there is something to learn from."
          : " The nightly pass records one once the adaptive runtime runs, from cutover (P6); until then the targets below are what would be published now.",
      );
    }
    return h(
      "dl",
      { class: "snapshot" },
      h("dt", {}, "In force since"),
      h("dd", {}, s.valid_from),
      h("dt", {}, "From the pass at"),
      h("dd", {}, `${s.ran_at} (${view.clock.time_zone})`),
      h("dt", {}, "State"),
      h("dd", {}, s.mode === "learning" ? "Learning" : "Bootstrap"),
      h("dt", {}, "Days analysed"),
      h("dd", {}, String(s.days_analysed)),
    );
  }

  function almanacTable(view) {
    const units = view.units;
    const knobs = [
      ["Lux target", (s) => luxText(s.lux_target, s.lux_source)],
      ["Maintain", (s) => (s.knobs.maintenance_enabled ? "on" : "off")],
      ["Margin", (s) => `${s.knobs.lux_margin} lx`],
      ["Max step", (s) => `${Number((s.knobs.max_step_pct * 100).toFixed(2))}%`],
      ["Fade", (s) => `${s.knobs.transition_seconds} s`],
    ];
    return [
      h(
        "p",
        { class: "muted small" },
        "Each learning unit’s target as the almanac would publish it (Off holds the unit off; a brightness brings it on there; nothing learned leaves it at its last brightness), and each section’s knobs as the publish reads them: the room’s config as it is now.",
      ),
      units.length
        ? null
        : h("p", { class: "muted none" }, "No learning units yet: every bulb in this room is switched-only."),
      h(
        "div",
        { class: "matrix-wrap" },
        h(
          "table",
          { class: "almanac-table" },
          h(
            "thead",
            {},
            h(
              "tr",
              {},
              h("th", { scope: "col", class: "section-head" }, "Section"),
              units.map((u) => h("th", { scope: "col", dataset: { unit: u.id } }, u.name)),
              knobs.map(([title], i) => h("th", { scope: "col", class: `knob-head${i ? "" : " first"}` }, title)),
            ),
          ),
          h(
            "tbody",
            {},
            view.sections.map((s) =>
              h(
                "tr",
                { dataset: { section: s.section } },
                h("th", { scope: "row", class: "section-name" }, sectionName(s.section)),
                units.map((u) => {
                  const cell = s.units[u.id];
                  return h(
                    "td",
                    { dataset: { unit: u.id }, class: "target-cell" },
                    targetText(cell.target),
                    cell.learned !== null && cell.learned !== cell.target
                      ? h("span", { class: "muted small block" }, `learned ${cell.learned}`)
                      : null,
                    cell.valid_from ? h("span", { class: "muted small block" }, `since ${cell.valid_from}`) : null,
                  );
                }),
                knobs.map(([title, value], i) =>
                  h("td", { class: i ? null : "first", dataset: { knob: title } }, value(s)),
                ),
              ),
            ),
          ),
        ),
      ),
    ];
  }
})();
