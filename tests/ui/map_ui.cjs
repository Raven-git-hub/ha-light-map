// Drives the Map tab in headless Chromium against a running Light Map over a fake HA
// (started by tests/test_ui.py): loads the map, checks the fixture renders, and exercises
// the key interactions. Pragmatic DOM checks, not a browser suite. Prints one JSON line
// ({ok, steps, errors}) and exits non-zero on failure.
//
//   node tests/ui/map_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

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
    // A 4xx the UI handles still logs "Failed to load resource"; anything else is a bug.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  try {
    await step("the map renders the fixture house", async () => {
      await page.goto(`${base}/#map`);
      await page.waitForSelector("table.map-table");
      const rooms = await page.$$eval("tbody.room .room-name", (els) => els.map((e) => e.textContent));
      assert.deepEqual(rooms, ["Kitchen", "Living Room", "Unassigned"]);
      assert.equal(await page.textContent('tr[data-bulb="porch"] button.soft'), "input_boolean.lm_soft_porch");
      assert.match(await page.textContent('tr[data-bulb="porch"] .bulb-cell'), /light\.porch/);
      // Live: reading_lamp directly, the chandeliers through the living_all group.
      assert.ok(await page.$('tr[data-bulb="reading_lamp"] .badge.live'));
      assert.ok(await page.$('tr[data-bulb="chandelier_2"] .badge.live'));
      assert.equal(await page.$('tr[data-bulb="kitchen_strip"] .badge.live'), null);
      assert.ok(await page.$('button.group[data-group="living_all"] .badge.live'));
      assert.match(await page.textContent('button.group[data-group="living_all"]'), /Chandelier 1, Chandelier 2/);
      assert.ok(await page.isDisabled('tbody[data-room="kitchen"] button.adaptive-config'));
      assert.ok(await page.isEnabled('tbody[data-room="living_room"] button.adaptive-config'));
      assert.ok(await page.isChecked('tbody[data-room="living_room"] input.adaptive-flag'));
      await page.waitForFunction(() => document.querySelector("#sync-state").textContent.startsWith("Stale"));
      assert.ok(await page.$(".coexistence-banner"));
      assert.equal(await page.getAttribute('a[href="#map"]', "aria-current"), "page");
      await shot(page, "01-map");
    });

    await step("a toggle on a mapped bulb is refused in the form", async () => {
      await page.click('tr[data-bulb="porch"] button.soft');
      await page.waitForSelector(`${dialog("hard-switches")} .add-binding`);
      await page.click(`${dialog("hard-switches")} .add-binding`);
      await page.fill(".binding-name", "Loop");
      await page.fill(".source-entity", "light.kitchen_strip");
      await page.click(".done-binding");
      assert.match(await page.textContent(".binding-form .form-error"), /A toggle can.t take a bulb/);
      await page.click(".cancel-binding");
    });

    await step("binding a hard switch warns about coexistence before saving", async () => {
      const popup = dialog("hard-switches");
      await page.click(`${popup} .add-binding`);
      await page.fill(".binding-name", "Porch button");
      await page.fill(".source-entity", "switch.reading_relay");
      // The device trigger is the advanced path, folded away until asked for.
      assert.equal(await page.isVisible(".source-device-id"), false);
      await page.click(".done-binding");
      assert.match(await page.textContent(`${popup} .bindings-table`), /entity · switch\.reading_relay/);
      assert.ok(await page.isVisible(`${popup} .coexistence`));
      assert.match(await page.textContent(`${popup} .coexistence`), /ha-adaptive-light/);
      assert.ok(await page.isDisabled(`${popup} button.save`));
      await shot(page, "02-hard-switch");
      await page.check(`${popup} .coexistence input.ack`);
      assert.ok(await page.isEnabled(`${popup} button.save`));
      await page.click(`${popup} button.save`);
      await page.waitForSelector(popup, { state: "detached" });
      await page.waitForSelector('tr[data-bulb="porch"] .badge.live');
      assert.match(await page.textContent('tr[data-bulb="porch"] .switch-count'), /1 hard switch/);
    });

    await step("an event source and a press, with no warning for an unchanged list", async () => {
      await page.click('tr[data-bulb="kitchen_strip"] button.soft');
      const popup = dialog("hard-switches");
      await page.click(`${popup} .add-binding`);
      await page.fill(".binding-name", "Remote off");
      await page.check('input[value="event"]');
      assert.ok(await page.isVisible(".source-event-type"));
      assert.ok(await page.isDisabled('input[value="follow"]'));
      await page.fill(".source-event-type", "zha_event");
      await page.fill(".source-event-data", '{"command": "off"}');
      await page.check('input[value="press"]');
      await page.selectOption(".turn", "off");
      await page.click(".done-binding");
      assert.match(await page.textContent(`${popup} .bindings-table`), /event · zha_event \{"command":"off"\}/);
      assert.match(await page.textContent(`${popup} .bindings-table`), /press → off/);
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
      assert.equal(await page.$('tr[data-bulb="kitchen_strip"] .badge.live'), null);
    });

    await step("create a switching group", async () => {
      await page.click('tbody[data-room="kitchen"] button.add-group');
      const popup = dialog("group");
      await page.waitForSelector(popup);
      assert.ok(await page.isDisabled(`${popup} button.save`));
      await page.fill(".group-name-input", "Kitchen all");
      await page.check(`${popup} .member input[value="kitchen_strip"]`);
      assert.equal(await page.isVisible(`${popup} .coexistence`), false);
      await page.click(`${popup} button.save`);
      await page.waitForSelector(popup, { state: "detached" });
      await page.waitForSelector('tbody[data-room="kitchen"] button.group[data-group="kitchen_all"]');
      assert.match(await page.textContent('button.group[data-group="kitchen_all"]'), /Kitchen strip/);
    });

    await step("a popup another tab made stale closes instead of reverting the other edit", async () => {
      await page.click('tr[data-bulb="chandelier_1"] button.soft');
      const popup = dialog("hard-switches");
      await page.waitForSelector(`${popup} .add-binding`);
      const status = await page.evaluate(async () => {
        const map = await (await fetch("/api/map")).json();
        const bulb = map.rooms.flatMap((r) => r.bulbs).find((b) => b.id === "chandelier_1");
        const response = await fetch("/api/bulbs/chandelier_1/hard_switches", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base: bulb.version,
            acknowledge_live: true,
            hard_switches: [
              { name: "Other tab", source: { type: "event", event_type: "zha_event" }, mode: "press", turn: "off" },
            ],
          }),
        });
        return response.status;
      });
      assert.equal(status, 200);
      await page.click(`${popup} button.save`); // the popup's own, now stale, empty list
      await page.waitForSelector(popup, { state: "detached" });
      assert.match(await page.textContent("#notices .notice.error"), /changed since the Map showed it/);
      await page.waitForFunction(() =>
        document.querySelector('tr[data-bulb="chandelier_1"] .switch-count').textContent.startsWith("1 hard switch"),
      );
    });

    await step("move a bulb, previewed first", async () => {
      await page.click('tr[data-bulb="porch"] .move');
      const popup = dialog("move");
      await page.waitForSelector(popup);
      assert.ok(await page.isDisabled(`${popup} .confirm-move`));
      await page.selectOption(`${popup} .move-target`, "kitchen");
      await page.waitForFunction(() => document.querySelector(".move-preview").textContent.includes("bulb moved"));
      assert.match(
        await page.textContent(`${popup} .move-preview`),
        /bulb moved: light\.porch: unassigned -> room 'kitchen'/,
      );
      await page.click(`${popup} .confirm-move`);
      await page.waitForSelector(popup, { state: "detached" });
      await page.waitForSelector('tbody[data-room="kitchen"] tr[data-bulb="porch"]');
    });

    await step("a move HA would refuse is shown, not offered", async () => {
      await page.click('tr[data-bulb="chandelier_1"] .move');
      const popup = dialog("move");
      await page.selectOption(`${popup} .move-target`, "");
      await page.waitForSelector(`${popup} .refused`);
      assert.match(await page.textContent(`${popup} .refused`), /inherits Area 'living_room' from its device/);
      assert.ok(await page.isDisabled(`${popup} .confirm-move`));
      await page.click(`${popup} button.cancel`);
    });

    await step("the adaptive checkbox and the P5 placeholder", async () => {
      await page.check('tbody[data-room="kitchen"] input.adaptive-flag');
      await page.waitForSelector('tbody[data-room="kitchen"] button.adaptive-config:not([disabled])');
      await page.click('tbody[data-room="kitchen"] button.adaptive-config');
      await page.waitForSelector(dialog("adaptive"));
      assert.match(await page.textContent(dialog("adaptive")), /arrives in P5/);
      await page.keyboard.press("Escape");
      await page.waitForSelector(dialog("adaptive"), { state: "detached" });
      // Unticking a room whose adaptive config holds something asks first.
      await page.uncheck('tbody[data-room="living_room"] input.adaptive-flag');
      await page.waitForSelector(dialog("confirm"));
      assert.match(await page.textContent(dialog("confirm")), /2 learning unit\(s\)/);
      await page.click(`${dialog("confirm")} button:has-text("Cancel")`);
      assert.ok(await page.isChecked('tbody[data-room="living_room"] input.adaptive-flag'));
    });

    await step("sync from HA shows the changes before saving them", async () => {
      await page.click("#sync-button");
      const popup = dialog("sync");
      await page.waitForFunction(() => document.querySelector('dialog[data-dialog="sync"] h3'));
      assert.match(await page.textContent(popup), /room added: 'garage' \(Garage, Area garage\)/);
      await shot(page, "03-sync");
      await page.click(`${popup} .save-sync`);
      await page.waitForSelector(popup, { state: "detached" });
      await page.waitForSelector('tbody[data-room="garage"]');
      await page.waitForFunction(() => document.querySelector("#sync-state").textContent === "Synced with HA");
    });

    await step("the deploy preview, then an explicit deploy", async () => {
      await page.click("#deploy-button");
      const popup = dialog("deploy");
      await page.waitForSelector(`${popup} .plan-table.steps`);
      const text = await page.textContent(popup);
      assert.match(text, /create\s*soft switch\s*lm_soft_porch/);
      assert.match(text, /lm_hard_porch_button/);
      assert.match(text, /Crossover report/);
      assert.match(text, /light\.living_room_group/);
      assert.match(text, /Coexistence\./);
      assert.ok(await page.isVisible(`${popup} .going-live`));
      assert.ok(await page.isDisabled(`${popup} .deploy-confirm`));
      await shot(page, "04-deploy-preview");
      await page.check(`${popup} input.ack`);
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForSelector(`${popup} .deploy-result`);
      assert.match(await page.textContent(`${popup} .deploy-result`), /Deployed and verified/);
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
    });

    await step("the Area-write preview is its own action", async () => {
      await page.click("#areas-button");
      const popup = dialog("areas");
      await page.waitForFunction(() => document.querySelector('dialog[data-dialog="areas"] .box.ok'));
      assert.match(await page.textContent(popup), /already match the snapshot/);
      assert.ok(await page.isDisabled(`${popup} .areas-confirm`));
      await page.keyboard.press("Escape");
    });

    await step("ignoring a bulb that drops nothing is immediate", async () => {
      assert.match(await page.textContent(".ignored-section summary"), /Ignored \(0\)/);
      assert.equal(await page.getAttribute(".ignored-section", "open"), null); // collapsed
      await page.click('tr[data-bulb="counter_lamp"] .ignore');
      await page.waitForSelector('tr[data-bulb="counter_lamp"]', { state: "detached" });
      assert.match(await page.textContent("#notices .notice.ok"), /Deploy to remove its soft switch and bridge/);
      assert.match(await page.textContent(".ignored-section summary"), /Ignored \(1\)/);
      assert.ok(await page.$('.ignored-section tr[data-ignored="counter_lamp"]'));
      assert.match(await page.textContent('tr[data-ignored="counter_lamp"] .ignored-room'), /Kitchen/);
    });

    await step("ignoring a bulb that drops something asks first", async () => {
      await page.click('tr[data-bulb="kitchen_strip"] .ignore');
      await page.waitForSelector(dialog("confirm"));
      assert.match(
        await page.textContent(dialog("confirm")),
        /switching group 'Kitchen all', which would have no members left/,
      );
      await page.click(`${dialog("confirm")} button:has-text("Cancel")`);
      await page.waitForSelector(dialog("confirm"), { state: "detached" });
      assert.ok(await page.$('tr[data-bulb="kitchen_strip"]'));
      await page.click('tr[data-bulb="kitchen_strip"] .ignore');
      await page.click(`${dialog("confirm")} button.confirm`);
      await page.waitForSelector('tr[data-bulb="kitchen_strip"]', { state: "detached" });
      assert.equal(await page.$('button.group[data-group="kitchen_all"]'), null); // left empty, dropped
      assert.match(await page.textContent(".ignored-section summary"), /Ignored \(2\)/);
    });

    await step("ignored lights are offered by no picker", async () => {
      await page.click('tbody[data-room="kitchen"] button.add-group');
      const popup = dialog("group");
      await page.waitForSelector(popup);
      const members = await page.$$eval(`${popup} .member input`, (els) => els.map((e) => e.value));
      assert.deepEqual(members, ["porch"]);
      const sources = await page.$$eval("#lm-entities option", (els) => els.map((e) => e.value));
      assert.ok(sources.includes("switch.reading_relay"));
      assert.ok(!sources.includes("light.kitchen_strip") && !sources.includes("light.counter_lamp"));
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
    });

    await step("the next deploy removes the ignored bulbs' soft switches and bridges", async () => {
      await page.click("#deploy-button");
      const popup = dialog("deploy");
      await page.waitForSelector(`${popup} .plan-table.steps`);
      const text = await page.textContent(popup);
      for (const bulb of ["counter_lamp", "kitchen_strip"]) {
        assert.match(text, new RegExp(`remove\\s*soft switch\\s*lm_soft_${bulb}`));
        assert.match(text, new RegExp(`remove\\s*automation\\s*lm_bridge_${bulb}`));
      }
      assert.doesNotMatch(text, /create\s*\S+\s*lm_\S*counter_lamp/);
      await page.click(`${popup} .deploy-confirm`);
      await page.waitForSelector(`${popup} .deploy-result`);
      assert.match(await page.textContent(`${popup} .deploy-result`), /removed 4/);
      await page.click(`${popup} button.cancel`);
      await page.waitForSelector(popup, { state: "detached" });
    });

    await step("un-ignore from the Ignored section", async () => {
      await page.click(".ignored-section summary");
      await page.waitForSelector('.ignored-section[open] tr[data-ignored="counter_lamp"] .unignore');
      await shot(page, "05-ignored");
      await page.click('tr[data-ignored="counter_lamp"] .unignore');
      await page.waitForSelector('tbody[data-room="kitchen"] tr[data-bulb="counter_lamp"]');
      assert.match(await page.textContent("#notices .notice.ok:last-child"), /back in Kitchen/);
      assert.match(await page.textContent(".ignored-section summary"), /Ignored \(1\)/);
      // The section stays unfolded across the reload.
      assert.ok(await page.$('.ignored-section[open] tr[data-ignored="kitchen_strip"]'));
      assert.equal(await page.$('tr[data-ignored="counter_lamp"]'), null);
    });

    await step("the other tabs are placeholders", async () => {
      await page.click('a[href="#now"]');
      await page.waitForSelector(".placeholder");
      assert.match(await page.textContent(".placeholder"), /P5/);
      await page.click('a[href="#map"]');
      await page.waitForSelector("table.map-table");
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
