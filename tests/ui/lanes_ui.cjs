// Drives the Map's deploy dialog in headless Chromium against a running Light Map with the
// cutover switch on (wired by tests/test_ui.py in its own scope; the live app keeps it off
// until P6b): the adaptive al_ lane shows after the plan, first refused (the switching
// plane isn't deployed, so its soft switches are missing) and so not applied by the lm_
// deploy; then as steps the next deploy applies after lm_ (P6a), even with nothing left
// for lm_ to do; then as nothing to do. Prints one JSON line ({ok, steps, errors}) and
// exits non-zero on failure.
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
      assert.match(text, /Plan: the lm_ lane, first/);
      assert.match(text, /Module lanes, after lm_/);
      assert.match(text, /applies each lane shown ready here after the lm_ lane/);
      assert.match(await page.textContent(`${popup} .module-lane`), /al_ would be refused, so this deploy won’t apply it/);
      assert.match(await page.textContent(`${popup} .module-lane`), /deploy the switching plane first/);
      await shot(page, "lanes-01-refused");
    });

    await step("the deploy applies lm_ and says al_ wasn't, since it was refused", async () => {
      await page.check(`${popup} input.ack`);
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForSelector(`${popup} .deploy-result`);
      assert.match(await page.textContent(`${popup} .deploy-result`), /Deployed and verified\./);
      assert.match(await page.textContent(`${popup} .module-result`), /al_ was not applied: it was refused in the preview/);
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
    });

    await step("the al_ plan then lists its steps", async () => {
      await page.click("#deploy-button");
      await page.waitForSelector(`${popup} .module-lane li`);
      assert.match(await page.textContent(popup), /The lm_ lane already matches the config: nothing to do there/);
      assert.match(await page.textContent(popup), /then each module lane’s helpers and automations/);
      const lane = await page.textContent(`${popup} .module-lane`);
      assert.match(lane, /al_: 12 steps, applied after lm_/);
      assert.match(lane, /create select al_scene_living_room/);
      assert.match(lane, /create automation al_maint_living_room/);
      await shot(page, "lanes-02-planned");
      // Nothing is left for lm_, but the deploy still has al_'s steps to take.
      assert.equal(await page.$eval(`${popup} .deploy-confirm`, (b) => b.disabled), false);
    });

    await step("a deploy a module lane stops partway says what stands and shows what is left", async () => {
      // HA's answer when al_ fails after lm_ went out (as the server words it), stubbed: the
      // dialog must not leave the old preview standing as if nothing had happened.
      await page.route("**/api/deploy", (route) =>
        route.fulfill({
          status: 502,
          contentType: "application/json",
          body: JSON.stringify({
            error: "deploy_failed",
            detail: "Deployed and verified: lm_. The al_ lane's deploy stopped or did not verify.",
            problems: ["automation al_maint_living_room did not load: HA rejected it (see HA's log)"],
            deployed: ["lm"],
            result: { prefix: "lm", created: {}, updated: {}, removed: {}, sync: [], synced: {}, unsynced: {}, warnings: [] },
            modules: {},
          }),
        }),
      );
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForFunction((sel) => /Part of this deploy went through/.test(document.querySelector(sel).textContent), popup);
      const text = await page.textContent(popup);
      assert.match(text, /Deployed and verified: lm_\. The al_ lane's deploy stopped/);
      assert.match(text, /al_maint_living_room did not load/);
      assert.match(await page.textContent(`${popup} .module-lane`), /al_: 12 steps, applied after lm_/);
      await shot(page, "lanes-03-partial");
      await page.unroute("**/api/deploy");
    });

    await step("the deploy then applies al_ after lm_", async () => {
      assert.equal(await page.$eval(`${popup} .deploy-confirm`, (b) => b.disabled), false);
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForSelector(`${popup} .module-result`);
      const result = await page.textContent(`${popup} .module-result`);
      assert.match(result, /The al_ lane: deployed and verified\./);
      assert.match(result, /Created 12, updated 0, removed 0\./);
      await shot(page, "lanes-04-deployed");
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
    });

    await step("then nothing is left to do in either lane", async () => {
      await page.click("#deploy-button");
      await page.waitForSelector(`${popup} .module-lane`);
      assert.match(await page.textContent(`${popup} .module-lane`), /al_: nothing to do/);
      assert.equal(await page.$eval(`${popup} .deploy-confirm`, (b) => b.disabled), true);
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
