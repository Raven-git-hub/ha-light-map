// Drives the Map's deploy dialog in headless Chromium against a running Light Map with the
// cutover switch on (wired by tests/test_ui.py in its own scope; the live app keeps it off
// until P6): the adaptive al_ lane shows read-only beside the plan, first refused (the
// switching plane isn't deployed, so its soft switches are missing), then, after the lm_
// deploy, as steps that the dialog doesn't apply. Prints one JSON line ({ok, steps,
// errors}) and exits non-zero on failure.
//
//   node tests/ui/lanes_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

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

(async () => {
  const browser = await chromium.launch(process.env.LM_CHROMIUM ? { executablePath: process.env.LM_CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  const popup = dialog("deploy");
  try {
    await step("the al_ lane is shown, refused until the switching plane is deployed", async () => {
      await page.goto(`${base}/#map`);
      await page.waitForSelector("table.map-table");
      await page.click("#deploy-button");
      await page.waitForSelector(`${popup} .module-lane`);
      const text = await page.textContent(popup);
      assert.match(text, /Module lanes \(read-only\)/);
      assert.match(text, /applies the lm_ lane only/);
      assert.match(await page.textContent(`${popup} .module-lane`), /al_ would be refused/);
      assert.match(await page.textContent(`${popup} .module-lane`), /deploy the switching plane first/);
      await shot(page, "lanes-01-refused");
    });

    await step("the deploy applies lm_ only; the al_ plan then lists its steps", async () => {
      await page.check(`${popup} input.ack`);
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForSelector(`${popup} .deploy-result`);
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
      await page.click("#deploy-button");
      await page.waitForSelector(`${popup} .module-lane li`);
      const lane = await page.textContent(`${popup} .module-lane`);
      assert.match(lane, /al_: 12 steps, not applied here/);
      assert.match(lane, /create select al_scene_living_room/);
      assert.match(lane, /create automation al_maint_living_room/);
      await shot(page, "lanes-02-planned");
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
