// Drives the Log tab in headless Chromium against a running Light Map whose event log holds
// fixture entries (seeded by tests/test_ui.py): the table renders them newest first, the
// filters narrow it, a detail opens, and the status line says what is observed. Prints one
// JSON line ({ok, steps, errors}) and exits non-zero on failure.
//
//   node tests/ui/log_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

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

const messages = (page) => page.$$eval("table.log-table tbody td.message .text", (els) => els.map((e) => e.textContent));

// Pick a filter and wait until the table shows the result: #log-body names the query it
// rendered.
async function choose(page, selector, value, shown) {
  await page.selectOption(selector, value);
  await page.waitForFunction((want) => (document.querySelector("#log-body").dataset.view || "").includes(want), shown);
}

(async () => {
  const browser = await chromium.launch(process.env.LM_CHROMIUM ? { executablePath: process.env.LM_CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  try {
    await step("the log renders the fixture entries, newest first, info and up", async () => {
      await page.goto(`${base}/#log`);
      await page.waitForSelector("table.log-table");
      assert.deepEqual(await messages(page), [
        "Home Assistant rejected the token, so observation has stopped.",
        "Skipped a heartbeat: a guard was still raised.",
        "Manual change in Living Room: chandelier_1.",
      ]);
      assert.equal(await page.textContent('table.log-table tr.level-info td.room'), "Living Room");
      assert.equal(await page.textContent("table.log-table tr.level-error td.level-cell"), "error");
      assert.equal(await page.getAttribute('a[href="#log"]', "aria-current"), "page");
      if (shots) await page.screenshot({ path: path.join(shots, "log-01.png"), fullPage: true });
    });

    await step("a detail opens", async () => {
      await page.click("table.log-table tr.level-info details summary");
      assert.match(await page.textContent("table.log-table tr.level-info details pre"), /"chandelier_1"/);
    });

    await step("the filters narrow it", async () => {
      await choose(page, "#log-level", "debug", "level=debug");
      assert.equal((await messages(page)).length, 4);
      await choose(page, "#log-category", "heartbeat", "category=heartbeat");
      assert.deepEqual(await messages(page), [
        "Skipped a heartbeat: a guard was still raised.",
        "Heartbeat: 2 room(s), 5 bulb(s), 2 on.",
      ]);
      await choose(page, "#log-room", "kitchen", "room=kitchen");
      assert.deepEqual(await messages(page), ["Skipped a heartbeat: a guard was still raised."]);
      // Every room of the config, by name, whether or not it has entries yet.
      const rooms = await page.$$eval("#log-room option", (els) => els.map((e) => e.textContent));
      assert.deepEqual(rooms, ["All rooms", "Kitchen", "Living Room"]);
      await choose(page, "#log-level", "error", "level=error");
      assert.equal(await page.textContent("#log-body"), "Nothing has been logged that matches these filters.");
    });

    await step("the status line says what is observed", async () => {
      await page.waitForFunction(() => document.querySelector("#log-status").textContent.length > 0);
      assert.match(await page.textContent("#log-status"), /Not observing: observation is off for this app/);
      assert.match(await page.textContent("#log-status"), /0 heartbeats and 0 manual changes recorded/);
    });

    await step("the filters survive a trip to the Map", async () => {
      await page.click('a[href="#map"]');
      await page.waitForSelector("table.map-table");
      await page.click('a[href="#log"]');
      await page.waitForSelector("#log-level");
      assert.equal(await page.inputValue("#log-level"), "error");
      assert.equal(await page.inputValue("#log-room"), "kitchen");
    });
  } catch (err) {
    errors.push(err.stack || String(err));
  } finally {
    await browser.close();
  }
  const ok = errors.length === 0;
  console.log(JSON.stringify({ ok, steps, errors }));
  process.exit(ok ? 0 : 1);
})();
