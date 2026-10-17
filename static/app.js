// Light Map UI shell: the tab router, the API helper, and the DOM, notice and dialog helpers
// every view shares. Vanilla JS, no build step.
//
// A view registers itself with LM.view(name, render): map.js is the Map tab (P3), log.js
// the Log (P4), config.js the Config tab (P5.0: the adaptive rooms and the per-room
// adaptive editor). Now, Analysis and Almanac arrive with the adaptive engine (P5.2); until
// then each shows a placeholder.
//
// A route is `#view` or `#view/arg` (`#config/living_room` opens that room's editor):
// render(main, arg) gets the arg, decoded, or null. A view holding unsaved edits says so
// with LM.protect(dirty): leaving it (a tab, a link, the back button) asks first, and so
// does closing or reloading the page.

"use strict";

const LM = (() => {
  const views = {};

  // DOM without innerHTML: names come from Home Assistant, and text set as a text node
  // can never turn into markup.
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "value") el.value = value;
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, value);
    }
    return add(el, children);
  }

  // Children as h() takes them: nested arrays flattened, null/undefined/false skipped,
  // anything else a text node. (The DOM's own append() would print "null".)
  function add(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // Replace an element's children, taking them as h() does.
  function fill(el, ...children) {
    el.replaceChildren();
    return add(el, children);
  }

  // -- The API -------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, body) {
      super(body.detail || `HTTP ${status}`);
      this.status = status;
      this.body = body;
      this.error = body.error;
      this.problems = body.problems || [];
    }
  }

  // Every call goes through here, so a failure always arrives as an ApiError carrying
  // the server's {error, detail, problems}.
  async function api(method, path, body) {
    const init = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new ApiError(0, { error: "network", detail: `Light Map did not answer (${err.message})` });
    }
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: `${response.status} ${response.statusText}` };
    }
    if (!response.ok) throw new ApiError(response.status, data || {});
    return data;
  }

  // -- Notices -------------------------------------------------------------------------

  function notify(text, { kind = "info", problems = [], timeout } = {}) {
    const box = document.getElementById("notices");
    // One success at a time: the latest says all that matters. Errors stay until dismissed.
    if (kind === "ok") for (const old of box.querySelectorAll(".notice.ok")) old.remove();
    const notice = h(
      "div",
      { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" },
      h(
        "div",
        { class: "text" },
        text,
        problems.length
          ? h(
              "ul",
              {},
              problems.map((p) => h("li", {}, p)),
            )
          : null,
      ),
      h("button", { class: "icon", "aria-label": "Dismiss", onclick: () => notice.remove() }, "×"),
    );
    box.append(notice);
    if (timeout) setTimeout(() => notice.remove(), timeout);
    return notice;
  }

  function fail(err) {
    console.error(err);
    notify(err.message || String(err), { kind: "error", problems: err.problems || [] });
  }

  function clearNotices() {
    document.getElementById("notices").replaceChildren();
  }

  // -- Dialogs -------------------------------------------------------------------------

  // A modal on a native <dialog>. `build(close)` returns its body; `close(value)` removes
  // it and resolves the promise with `value` (undefined when dismissed).
  //
  // `close.during(promise)` marks the dialog busy while a request runs: dismissing it
  // (Esc, ×, Cancel) is ignored until the request settles. Dismissed mid-save, the dialog
  // would resolve undefined, the save's own result would be dropped, and the Map would go
  // on showing what was there before a write that did land.
  function dialog(title, build, { wide = false, name } = {}) {
    return new Promise((resolve) => {
      const el = h("dialog", { class: wide ? "modal wide" : "modal", dataset: { dialog: name || "" } });
      let settled = false;
      let busy = 0;
      const close = (value) => {
        if (settled || (value === undefined && busy)) return;
        settled = true;
        el.close();
        el.remove();
        resolve(value);
      };
      close.during = async (promise) => {
        busy += 1;
        el.classList.add("busy");
        try {
          return await promise;
        } finally {
          busy -= 1;
          if (!busy) el.classList.remove("busy");
        }
      };
      el.addEventListener("cancel", (event) => {
        event.preventDefault();
        close(undefined);
      });
      const body = h("div", { class: "modal-body" });
      el.append(
        h(
          "header",
          { class: "modal-head" },
          h("h2", {}, title),
          h("button", { class: "icon", "aria-label": "Close", onclick: () => close(undefined) }, "×"),
        ),
        body,
      );
      document.body.append(el);
      body.append(build(close, body));
      el.showModal();
    });
  }

  function footer(...buttons) {
    return h("footer", { class: "modal-foot" }, ...buttons);
  }

  // Resolves true only on an explicit confirm.
  function confirmBox(title, lines, { confirm = "Confirm", danger = false } = {}) {
    return dialog(
      title,
      (close) =>
        h(
          "div",
          {},
          lines.map((line) => (typeof line === "string" ? h("p", {}, line) : line)),
          footer(
            h("button", { onclick: () => close(false) }, "Cancel"),
            h("button", { class: danger ? "danger confirm" : "primary confirm", onclick: () => close(true) }, confirm),
          ),
        ),
      { name: "confirm" },
    ).then((value) => value === true);
  }

  function list(items, render = (item) => item) {
    return h(
      "ul",
      { class: "plain" },
      items.map((item) => h("li", {}, render(item))),
    );
  }

  // -- Router --------------------------------------------------------------------------

  const PLACEHOLDERS = {
    now: ["Now", "The live state of each adaptive room. Arrives with the Adaptive Light engine (P5.2)."],
    analysis: ["Analysis", "What the learner made of each learning unit. Arrives with the Adaptive Light engine (P5.2)."],
    almanac: [
      "Almanac",
      "The learned targets per learning unit and section. Arrives with the Adaptive Light engine (P5.2).",
    ],
  };

  function view(name, render) {
    views[name] = render;
  }

  // The view on screen's unsaved-edit check, and the route it was shown for. Set by the
  // view (protect), cleared by every route that leaves it.
  let guard = null;
  let shown = null;

  function protect(dirty, what = "Your changes") {
    guard = dirty ? { dirty, what } : null;
  }

  async function route() {
    const hash = location.hash || "#map";
    if (guard && hash !== shown && guard.dirty()) {
      const leave = await confirmBox(
        "Leave without saving?",
        [`${guard.what} haven’t been saved. Leaving drops them.`],
        { confirm: "Leave", danger: true },
      );
      if (!leave) {
        // Stay: put the address back without routing again (replaceState fires no
        // hashchange), so the view and its draft are exactly as they were.
        history.replaceState(null, "", shown);
        return;
      }
    }
    guard = null;
    shown = hash;
    const [name, ...rest] = hash.slice(1).split("/");
    const arg = rest.length ? decodeURIComponent(rest.join("/")) : null;
    for (const tab of document.querySelectorAll(".tab")) {
      if (tab.getAttribute("href") === `#${name || "map"}`) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    const main = document.getElementById("view");
    main.replaceChildren();
    clearNotices();
    const render = views[name || "map"];
    if (render) {
      try {
        await render(main, arg);
      } catch (err) {
        fail(err);
      }
      return;
    }
    const [title, text] = PLACEHOLDERS[name] || ["Not found", `There is no view called ${name}.`];
    main.append(h("section", { class: "placeholder" }, h("h2", {}, title), h("p", {}, text)));
  }

  async function showStatus() {
    const pill = document.getElementById("ha-status");
    try {
      const status = await api("GET", "/api/status");
      if (status.ha.configured) {
        pill.textContent = "Home Assistant configured";
        pill.className = "pill ok";
        pill.title = "";
      } else {
        pill.textContent = "Home Assistant not configured";
        pill.className = "pill error";
        pill.title = status.ha.error || "";
      }
    } catch (err) {
      pill.textContent = "Light Map unreachable";
      pill.className = "pill error";
      pill.title = err.message;
    }
  }

  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", (event) => {
    if (guard?.dirty()) {
      event.preventDefault();
      event.returnValue = ""; // older browsers ask only when this is set
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    route();
    showStatus();
  });

  return { h, fill, api, ApiError, notify, fail, clearNotices, dialog, footer, confirmBox, list, view, route, protect };
})();
