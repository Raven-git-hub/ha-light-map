// The Log tab (P4): the event log, read-only. What the platform did and saw (connects and
// drops, manual changes, skipped heartbeats, store rebuilds) and every action taken from the
// Map, newest first. Filtered by minimum level, category and room; "Older…" pages back. The
// line above the table says whether anything is being observed.
//
// Read from /api/log and /api/status (see app/main.py). Like the Map, every name is set as
// text, never as markup: messages and room names come from Home Assistant.

"use strict";

(() => {
  const { h, fill, api, fail, ApiError } = LM;

  const PAGE = 200;
  const LEVELS = [
    ["debug", "Everything (debug)"],
    ["info", "Info and up"],
    ["warning", "Warnings and errors"],
    ["error", "Errors only"],
  ];

  // Kept while switching tabs, so coming back shows the same view.
  const filters = { level: "info", category: "", room: "" };
  let entries = [];

  LM.view("log", async (main) => {
    main.append(
      h(
        "div",
        { class: "log-toolbar" },
        filter("log-level", "Level", "level", LEVELS),
        filter("log-category", "Category", "category", [["", "All categories"]]),
        filter("log-room", "Room", "room", [["", "All rooms"]]),
        h("button", { id: "log-refresh", onclick: () => refresh().catch(fail) }, "Refresh"),
        h("span", { class: "spacer" }),
      ),
      h("p", { id: "log-status", class: "muted log-status" }),
      h("div", { id: "log-body" }, h("p", { class: "muted" }, "Loading the event log…")),
    );
    await refresh();
  });

  async function refresh() {
    await Promise.all([load(), showStatus()]);
  }

  function filter(id, label, key, choices) {
    return h(
      "label",
      { class: "log-filter" },
      h("span", {}, label),
      h(
        "select",
        {
          id,
          onchange: (event) => {
            filters[key] = event.target.value;
            load().catch(fail);
          },
        },
        options(choices, filters[key]),
      ),
    );
  }

  function options(choices, current) {
    // A filter whose value has gone from the log (pruned, or a room since removed) stays
    // selectable, so the view doesn't silently change under the user.
    if (current && !choices.some(([value]) => value === current)) choices = [...choices, [current, current]];
    return choices.map(([value, text]) => h("option", { value, selected: value === current }, text));
  }

  function setOptions(id, key, choices) {
    const select = document.getElementById(id);
    if (!select) return;
    fill(select, options(choices, filters[key]));
    select.value = filters[key];
  }

  async function load({ older = false } = {}) {
    const body = document.getElementById("log-body");
    if (!body) return; // another tab is showing
    const params = new URLSearchParams({ level: filters.level, limit: String(PAGE) });
    if (filters.category) params.set("category", filters.category);
    if (filters.room) params.set("room", filters.room);
    const view = params.toString();
    if (older && entries.length) params.set("before", String(entries[entries.length - 1].id));
    let log;
    try {
      log = await api("GET", `/api/log?${params}`);
    } catch (err) {
      if (err instanceof ApiError && err.error === "store_unavailable") {
        fill(body, h("div", { class: "box error log-unavailable" }, err.message));
        return;
      }
      throw err;
    }
    entries = older ? entries.concat(log.entries) : log.entries;
    setOptions("log-category", "category", [["", "All categories"], ...log.categories.map((c) => [c, c])]);
    setOptions("log-room", "room", [["", "All rooms"], ...log.rooms.map((r) => [r.id, r.name])]);
    render(body, log.more);
    body.dataset.view = view; // which filters the table shows (the UI test waits on it)
  }

  function render(body, more) {
    if (!entries.length) {
      fill(body, h("p", { class: "muted log-empty" }, "Nothing has been logged that matches these filters."));
      return;
    }
    fill(
      body,
      h(
        "table",
        { class: "log-table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            ["Time", "Level", "Category", "Room", "Message"].map((title) => h("th", {}, title)),
          ),
        ),
        h("tbody", {}, entries.map(row)),
      ),
      more ? h("button", { id: "log-older", onclick: () => load({ older: true }).catch(fail) }, "Older…") : null,
    );
  }

  function row(entry) {
    return h(
      "tr",
      { class: `level-${entry.level}`, dataset: { id: String(entry.id) } },
      h("td", { class: "time", title: entry.ts }, when(entry.ts)),
      h("td", { class: "level-cell" }, h("span", { class: `level ${entry.level}` }, entry.level)),
      h("td", { class: "category" }, entry.category),
      h("td", { class: "room" }, entry.room_id ? entry.room_name || entry.room_id : ""),
      h(
        "td",
        { class: "message" },
        h("span", { class: "text" }, entry.message),
        entry.detail
          ? h("details", {}, h("summary", {}, "Detail"), h("pre", {}, JSON.stringify(entry.detail, null, 2)))
          : null,
      ),
    );
  }

  // In the browser's own time zone; the stored time (UTC) is the cell's tooltip.
  function when(ts) {
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return ts;
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  async function showStatus() {
    const line = document.getElementById("log-status");
    if (!line) return;
    try {
      line.textContent = describe(await api("GET", "/api/status"));
    } catch (err) {
      line.textContent = `Light Map's status is unavailable: ${err.message}`;
    }
  }

  function describe({ observer, store }) {
    if (!store.open) return `Nothing is observed or logged: ${store.error}`;
    const parts = [];
    if (!observer.running) parts.push(`Not observing: ${observer.reason}`);
    else if (!observer.rooms) parts.push("Waiting for a room with bulbs (sync from HA on the Map)");
    else if (!observer.connected) parts.push("Waiting for Home Assistant");
    else parts.push(`Observing ${plural(observer.rooms, "room")} and ${plural(observer.bulbs, "bulb")}`);
    parts.push(`${plural(store.heartbeats, "heartbeat")} and ${plural(store.reactive_events, "manual change")} recorded`);
    if (store.last_heartbeat) parts.push(`last heartbeat ${when(store.last_heartbeat)}`);
    if (store.stale) parts.push("the query layer is behind the archive and is rebuilt at the next start");
    return parts.join(" · ");
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }
})();
