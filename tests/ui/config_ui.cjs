// Drives the adaptive editor (the Config tab, P5.0) in headless Chromium against a running
// Light Map over a fake HA (started by tests/test_ui.py), whose living room starts with
// chandelier_1 and _2 adaptive in one unit, chandelier_3 (unavailable) and the reading lamp
// (which HA reports as on/off only) switched-only. Opens the editor from the Map, sees the
// lock, changes tiers, builds units, edits the matrix and the sensors, saves, and checks the
// concurrency and unsaved-changes guards and the phone layout. Pragmatic DOM checks, not a
// browser suite. Prints one JSON line ({ok, steps, errors}) and exits non-zero on failure.
//
//   node tests/ui/config_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");

const base = process.argv[2];
const shots = process.argv[3];
const steps = [];
const errors = [];

async function step(name, fn) {
  await fn();
  steps.push(name);
}

async function shot(page, name) {
  if (shots) await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
}

const dialog = (name) => `dialog[data-dialog="${name}"]`;
const row = (bulb) => `#adaptive-editor tr[data-bulb="${bulb}"]`;
const columns = (page) =>
  page.$$eval(".scene-matrix thead th.unit-head", (els) => els.map((e) => e.dataset.unit));
const units = (page) =>
  page.$$eval(".unit-card", (els) =>
    els.map((e) => [e.dataset.unit, e.querySelector(".unit-members").textContent]),
  );
const stateText = (page) => page.textContent("#editor-state");
const saved = (page) =>
  page.evaluate(async () => {
    const doc = await (await fetch("/api/config")).json();
    return doc.rooms.find((r) => r.id === "living_room").adaptive_config;
  });

(async () => {
  const browser = await chromium.launch(process.env.LM_CHROMIUM ? { executablePath: process.env.LM_CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (message) => {
    // A 4xx the UI handles still logs "Failed to load resource"; anything else is a bug.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  try {
    await step("the Map opens the room's editor", async () => {
      await page.goto(`${base}/#map`);
      await page.waitForSelector("table.map-table");
      assert.match(
        await page.textContent('tbody[data-room="living_room"] .adaptive-summary'),
        /2 adaptive bulbs in 1 learning unit/,
      );
      assert.ok(await page.$('tr[data-bulb="chandelier_1"] .tier-tag'));
      assert.equal(await page.$('tr[data-bulb="reading_lamp"] .tier-tag'), null);
      await page.click('tbody[data-room="living_room"] button.adaptive-config');
      await page.waitForSelector("#adaptive-editor .scene-matrix");
      assert.equal(new URL(page.url()).hash, "#config/living_room");
      assert.equal(await page.getAttribute('a[href="#config"].tab', "aria-current"), "page");
      assert.match(await page.textContent("#adaptive-editor h2"), /Adaptive config · Living Room/);
      assert.equal(await stateText(page), "Saved");
      assert.ok(await page.isDisabled("#editor-save"));
      assert.ok(await page.$("#adaptive-editor a.back-to-map"));
      assert.deepEqual(await columns(page), ["chandelier"]);
    });

    await step("a bulb that can't dim is locked; an offline one isn't", async () => {
      assert.ok(await page.isDisabled(`${row("reading_lamp")} .tier-adaptive`));
      assert.equal(await page.getAttribute(`${row("reading_lamp")} .tier-switched`, "aria-pressed"), "true");
      assert.match(await page.textContent(`${row("reading_lamp")} .capability.locked`), /only switches on and off/);
      assert.ok(await page.$(`${row("reading_lamp")} .badge.lock`));
      // chandelier_3 is unavailable: capability unknown, which never locks.
      assert.ok(await page.isEnabled(`${row("chandelier_3")} .tier-adaptive`));
      assert.match(await page.textContent(`${row("chandelier_3")} .notes-cell`), /Capability unknown \(offline\)/);
      // The group with the reading lamp says the lamp stays out.
      assert.match(
        await page.textContent('.learn-group-row[data-group="living_all"]'),
        /Reading lamp can.t dim and stays switched-only/,
      );
      assert.ok(await page.isDisabled('button.learn-group[data-group="reading_corner"]'));
      await shot(page, "01-editor");
    });

    await step("a bulb made adaptive gets a unit of its own", async () => {
      await page.click(`${row("chandelier_3")} .tier-adaptive`);
      await page.waitForSelector(`${row("chandelier_3")} .unit-select`);
      assert.equal(await page.inputValue(`${row("chandelier_3")} .unit-select`), "chandelier_3");
      assert.deepEqual(await columns(page), ["chandelier", "chandelier_3"]);
      assert.equal(await stateText(page), "Unsaved changes");
      assert.ok(await page.isEnabled("#editor-save"));
    });

    await step("build units: split one, merge across with the picker, then learn a group", async () => {
      await page.click('.unit-card[data-unit="chandelier"] .split-unit');
      assert.deepEqual(await columns(page), ["chandelier_3", "chandelier_1", "chandelier_2"]);
      await page.selectOption(`${row("chandelier_3")} .unit-select`, "chandelier_1");
      assert.deepEqual(await units(page), [
        ["chandelier_1", "Chandelier 1, Chandelier 3"],
        ["chandelier_2", "Chandelier 2"],
      ]);
      assert.deepEqual(await columns(page), ["chandelier_1", "chandelier_2"]);
      await page.click('button.learn-group[data-group="living_all"]');
      assert.deepEqual(await units(page), [["living_all", "Chandelier 1, Chandelier 2, Chandelier 3"]]);
      assert.deepEqual(await columns(page), ["living_all"]);
      assert.ok(await page.isDisabled('button.learn-group[data-group="living_all"]'));
      assert.match(await page.textContent('.learn-group-row[data-group="living_all"]'), /Already one learning unit/);
      // The locked lamp stayed out.
      assert.equal(await page.getAttribute(`${row("reading_lamp")} .tier-switched`, "aria-pressed"), "true");
      // Renamed: the id (the almanac key) stays.
      await page.fill('.unit-card[data-unit="living_all"] .unit-name-input', "Chandelier");
      await page.press('.unit-card[data-unit="living_all"] .unit-name-input', "Tab");
      await page.waitForFunction(
        () => document.querySelector('.scene-matrix th.unit-head[data-unit="living_all"]').textContent === "Chandelier",
      );
    });

    await step("edit the matrix: a mode, the knobs, and a bad value holds the save", async () => {
      await page.click('tr[data-section="night"] td.mode-cell[data-unit="living_all"] .mode-off');
      assert.equal(
        await page.getAttribute('tr[data-section="night"] td.mode-cell[data-unit="living_all"] .mode-off', "aria-pressed"),
        "true",
      );
      await page.fill('tr[data-section="sunrise"] .knob.knob-lux_margin', "8");
      await page.fill('tr[data-section="day"] .knob.knob-lux_target_seed', "300");
      await page.fill('tr[data-section="day"] .knob.knob-max_step_pct', "2.5");
      await page.uncheck('tr[data-section="afternoon"] .knob-maintenance_enabled');
      await page.fill('tr[data-section="sunset"] .knob-transition_seconds', "-5");
      assert.ok(await page.isDisabled("#editor-save"));
      assert.match(await page.textContent("#editor-problems"), /Sunset: Fade \(s\): must be more than or equal to 0/);
      assert.ok(await page.$('tr[data-section="sunset"] .knob-transition_seconds.invalid'));
      await page.fill('tr[data-section="sunset"] .knob-transition_seconds', "90");
      assert.ok(await page.isEnabled("#editor-save"));
      assert.equal(await page.textContent("#editor-problems"), "");
    });

    await step("sensors: a person can't be a presence sensor; a binary sensor can", async () => {
      const presence = '.sensor-list[data-field="presence_sensors"]';
      await page.fill(`${presence} .sensor-input`, "person.alex");
      await page.click(`${presence} .add-sensor`);
      assert.match(await page.textContent(`${presence} .form-error`), /doesn.t read on\/off/);
      await page.fill(`${presence} .sensor-input`, "binary_sensor.sofa");
      await page.click(`${presence} .add-sensor`);
      await page.waitForSelector(`${presence} .chip[data-sensor="binary_sensor.sofa"]`);
      await page.selectOption("#profile-select", "default");
      await shot(page, "02-edited");
    });

    await step("save, and the server has exactly the draft", async () => {
      await page.click("#editor-save");
      await page.waitForSelector("#notices .notice.ok");
      assert.match(await page.textContent("#notices .notice.ok"), /Saved Living Room.s adaptive config/);
      await page.waitForFunction(() => document.querySelector("#editor-state")?.textContent === "Saved");
      const config = await saved(page);
      assert.deepEqual(config.adaptive_bulbs, ["chandelier_1", "chandelier_2", "chandelier_3"]);
      assert.deepEqual(config.learning_units, [
        { id: "living_all", name: "Chandelier", members: ["chandelier_1", "chandelier_2", "chandelier_3"] },
      ]);
      assert.deepEqual(config.presence_sensors, ["binary_sensor.sofa"]);
      assert.equal(config.schedule_profile, "default");
      assert.deepEqual(config.scenes.night.units, { living_all: { mode: "off" } });
      assert.deepEqual(config.scenes.day.units, { living_all: { mode: "auto" } });
      assert.equal(config.scenes.sunrise.lux_margin, 8);
      assert.equal(config.scenes.day.lux_target_seed, 300);
      assert.equal(config.scenes.day.max_step_pct, 0.025);
      assert.equal(config.scenes.afternoon.maintenance_enabled, false);
      assert.equal(config.scenes.sunset.transition_seconds, 90);
    });

    await step("leaving with unsaved changes asks first", async () => {
      await page.selectOption("#profile-select", "evening");
      assert.equal(await stateText(page), "Unsaved changes");
      await page.click("#adaptive-editor a.back-to-map");
      await page.waitForSelector(dialog("confirm"));
      await page.click(`${dialog("confirm")} button:has-text("Cancel")`);
      await page.waitForSelector(dialog("confirm"), { state: "detached" });
      assert.equal(new URL(page.url()).hash, "#config/living_room");
      assert.equal(await page.inputValue("#profile-select"), "evening"); // the draft is kept
      await page.click("#editor-revert");
      await page.click(`${dialog("confirm")} button.confirm`);
      await page.waitForFunction(() => document.querySelector("#editor-state")?.textContent === "Saved");
      assert.equal(await page.inputValue("#profile-select"), "default");
    });

    await step("a stale editor reloads instead of overwriting, and the lock demotes", async () => {
      await page.click('tr[data-section="night"] td.mode-cell[data-unit="living_all"] .mode-auto');
      // Another tab (or a hand edit) makes the on/off-only lamp adaptive: the config can't
      // see HA, so the server takes it; only the editor knows better.
      const status = await page.evaluate(async () => {
        const view = await (await fetch("/api/rooms/living_room/adaptive_config")).json();
        const block = view.adaptive_config;
        block.adaptive_bulbs.push("reading_lamp");
        block.learning_units.push({ id: "reading", name: "Reading", members: ["reading_lamp"] });
        const response = await fetch("/api/rooms/living_room/adaptive_config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ adaptive_config: block, base: view.version }),
        });
        return response.status;
      });
      assert.equal(status, 200);
      await page.click("#editor-save"); // made against the older version
      await page.waitForSelector("#notices .notice.error");
      assert.match(await page.textContent("#notices .notice.error"), /changed since the editor showed it/);
      await page.waitForSelector(`${row("reading_lamp")} .demoted`);
      // Reloaded: the night change is gone (it would have reverted the other edit), and the
      // lamp is shown switched-only, which saving makes so.
      assert.equal(
        await page.getAttribute('tr[data-section="night"] td.mode-cell[data-unit="living_all"] .mode-off', "aria-pressed"),
        "true",
      );
      assert.equal(await page.getAttribute(`${row("reading_lamp")} .tier-switched`, "aria-pressed"), "true");
      assert.equal(await stateText(page), "Unsaved changes");
      assert.deepEqual(await columns(page), ["living_all"]);
      await shot(page, "03-demoted");
      await page.click("#editor-save");
      await page.waitForFunction(() => document.querySelector("#editor-state")?.textContent === "Saved");
      const config = await saved(page);
      assert.deepEqual(config.adaptive_bulbs, ["chandelier_1", "chandelier_2", "chandelier_3"]);
      assert.deepEqual(Object.keys(config.scenes.sleep.units), ["living_all"]);
    });

    await step("legible at phone width", async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      const layout = await page.evaluate(() => ({
        page: document.documentElement.scrollWidth,
        width: window.innerWidth,
        matrix: document.querySelector(".matrix-wrap").scrollWidth > document.querySelector(".matrix-wrap").clientWidth,
        rows: getComputedStyle(document.querySelector(".bulb-tiers tbody tr")).display,
      }));
      assert.ok(layout.page <= layout.width, `the page scrolls sideways: ${layout.page} > ${layout.width}`);
      assert.ok(layout.matrix, "the matrix scrolls in its own box");
      assert.equal(layout.rows, "block");
      await shot(page, "04-phone");
      await page.setViewportSize({ width: 1440, height: 1000 });
    });

    await step("the Config tab lists the adaptive rooms", async () => {
      await page.click('a.tab[href="#config"]');
      await page.waitForSelector(".adaptive-rooms .room-links");
      const listed = await page.$$eval(".room-links li", (els) => els.map((e) => e.dataset.room));
      assert.deepEqual(listed, ["living_room"]);
      assert.match(await page.textContent(".room-links"), /3 adaptive bulbs in 1 learning unit/);
      await page.click('.room-links a[href="#config/living_room"]');
      await page.waitForSelector("#adaptive-editor .scene-matrix");
    });

    await step("back to the Map", async () => {
      await page.click("#adaptive-editor a.back-to-map");
      await page.waitForSelector("table.map-table");
      assert.match(
        await page.textContent('tbody[data-room="living_room"] .adaptive-summary'),
        /3 adaptive bulbs in 1 learning unit/,
      );
      assert.ok(await page.$('tr[data-bulb="chandelier_3"] .tier-tag'));
    });

    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, steps, errors }));
  } catch (err) {
    await shot(page, "failure").catch(() => {});
    console.log(JSON.stringify({ ok: false, steps, errors, error: String(err.stack || err) }));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
