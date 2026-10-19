// Drives the schedule-profile editor (P5.2b) in headless Chromium against a running Light
// Map over a fake HA (started by tests/test_ui.py), whose living room follows the `evening`
// profile. Opens it from the Config tab, sees a profile's rooms and its day today, edits
// triggers (a collision shows as it is made; an invalid field holds Save), changes a
// trigger's shape, checks the leave guard, saves, adds and reorders a profile, has a stale
// save refused, and checks the phone layout. Pragmatic DOM checks, not a browser suite.
// Prints one JSON line ({ok, steps, errors}) and exits non-zero on failure.
//
//   node tests/ui/schedule_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

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
const item = (id) => `li.profile-item[data-profile="${id}"]`;
const row = (section) => `.sections-table tr[data-section="${section}"]`;
const today = (section) => `.sections-table td.today-cell[data-section="${section}"]`;
const listed = (page) => page.$$eval("li.profile-item", (lis) => lis.map((li) => li.dataset.profile));
const stateText = (page) => page.textContent("#schedule-state");
const waitToday = (page, section, pattern) =>
  page.waitForFunction(
    ([selector, source]) => new RegExp(source).test(document.querySelector(selector)?.textContent || ""),
    [today(section), pattern.source],
  );
const saved = (page) => page.evaluate(async () => (await (await fetch("/api/config")).json()).schedule_profiles);

(async () => {
  const browser = await chromium.launch(process.env.LM_CHROMIUM ? { executablePath: process.env.LM_CHROMIUM } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: "en-GB" });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (message) => {
    // A 4xx the UI handles still logs "Failed to load resource"; anything else is a bug.
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  try {
    await step("the Config tab opens the editor on the default profile", async () => {
      await page.goto(`${base}/#config`);
      await page.click("#edit-schedules");
      await page.waitForSelector(".profile-card .sections-table");
      assert.equal(new URL(page.url()).hash, "#schedule/default");
      assert.equal(await page.getAttribute('a[href="#config"].tab', "aria-current"), "page");
      assert.deepEqual(await listed(page), ["default", "evening"]);
      assert.equal(await page.getAttribute(`${item("default")} .select-profile`, "aria-current"), "true");
      assert.ok(await page.isDisabled(`${item("default")} .delete-profile`)); // always kept
      assert.equal(await stateText(page), "Saved");
      assert.ok(await page.isDisabled("#schedule-save"));
      assert.match(await page.textContent("#schedule-location"), /Today \(2026-10-16\).*Europe\/London/);
      // The six sections, in canonical order, each with its day today.
      const sections = await page.$$eval(".sections-table tbody tr", (trs) => trs.map((tr) => tr.dataset.section));
      assert.deepEqual(sections, ["sunrise", "day", "afternoon", "sunset", "night", "sleep"]);
      assert.equal((await page.textContent(today("night"))).trim(), "20:30");
    });

    await step("a profile names the rooms it is shared by", async () => {
      await page.click(`${item("evening")} .select-profile`);
      await page.waitForSelector('.profile-card[data-profile="evening"]');
      assert.equal(new URL(page.url()).hash, "#schedule/evening");
      assert.match(
        await page.textContent(".shared-warning"),
        /Used by one room: Living Room\. Saving changes the schedule of that room\./,
      );
      assert.ok(await page.isDisabled(`${item("evening")} .delete-profile`)); // a room follows it
      assert.match(await page.getAttribute(`${item("evening")} .delete-profile`, "title"), /Living Room follows it/);
      await shot(page, "01-schedule");
    });

    await step("an edit is checked as it is made: a collision shows before saving", async () => {
      // Sleep twenty minutes after night: inside the 30-minute minimum, so it is skipped.
      await page.fill(`${row("sleep")} .trigger-time`, "20:50");
      await waitToday(page, "sleep", /SKIPPED/);
      assert.match(await page.textContent(today("sleep")), /gap conflict: night/);
      assert.equal(await stateText(page), "Unsaved changes");
      await page.fill(`${row("sleep")} .trigger-time`, "22:30");
      await waitToday(page, "sleep", /^22:30$/);
      assert.ok(await page.isEnabled("#schedule-save"));
    });

    await step("an invalid field holds Save until it is fixed", async () => {
      await page.fill(`${row("day")} .trigger-offset`, "800");
      assert.ok(await page.$eval(`${row("day")} .trigger-offset`, (el) => el.classList.contains("invalid")));
      assert.match(
        await page.textContent("#schedule-problems"),
        /evening: Day: an offset is a whole number of minutes, −720 to 720/,
      );
      assert.ok(await page.isDisabled("#schedule-save"));
      await page.fill(`${row("day")} .trigger-offset`, "150");
      assert.equal((await page.textContent("#schedule-problems")).trim(), "");
      await waitToday(page, "day", /^09:55$/); // sunrise (07:25) + 2 h 30 min
      // A priority over the trigger's default (shown as the field's placeholder). One the
      // browser can't parse ("1e" reads as "") is invalid, never taken as "the default".
      assert.equal(await page.getAttribute(`${row("day")} .priority-input`, "placeholder"), "default");
      await page.click(`${row("day")} .priority-input`, { clickCount: 3 });
      await page.keyboard.type("1e");
      assert.ok(await page.$eval(`${row("day")} .priority-input`, (el) => el.classList.contains("invalid")));
      assert.match(await page.textContent("#schedule-problems"), /Day: a priority is a whole number/);
      assert.ok(await page.isDisabled("#schedule-save"));
      await page.fill(`${row("day")} .priority-input`, "120");
      assert.ok(await page.isEnabled("#schedule-save"));
    });

    await step("a trigger changes shape, and the profile is renamed", async () => {
      const type = `${row("night")} .trigger[data-depth="0"] > select.trigger-type`;
      // From the keyboard: each arrow changes the type and redraws, and the focus stays on
      // the select; coming back restores the trigger as it was.
      await page.focus(type);
      await page.keyboard.press("ArrowDown");
      await page.waitForSelector(`${row("night")} .trigger-event`);
      assert.equal(await page.evaluate(() => document.activeElement.dataset.focus), "evening:night:t:type");
      await page.keyboard.press("ArrowUp");
      await page.waitForSelector(`${row("night")} .trigger-time`);
      assert.equal(await page.inputValue(`${row("night")} .trigger-time`), "20:30");
      assert.equal(await page.evaluate(() => document.activeElement.dataset.focus), "evening:night:t:type");

      await page.selectOption(type, "earliest");
      await page.waitForSelector(`${row("night")} .components .trigger-time`);
      const times = await page.$$(`${row("night")} .components .trigger-time`);
      assert.equal(times.length, 2);
      assert.equal(await times[0].inputValue(), "20:30"); // the clock it was, kept as a component
      assert.ok(await page.isDisabled(`${row("night")} .remove-component`)); // two at least
      await times[0].fill("21:15");
      await times[1].fill("21:00");
      await waitToday(page, "night", /^21:00$/); // the earlier of the two, as checked
      await page.fill(".profile-name-input", "Evenings");
      assert.equal(await page.textContent(`${item("evening")} .profile-label`), "Evenings");
    });

    await step("leaving with unsaved changes asks first", async () => {
      await page.click('a[href="#map"].tab');
      await page.waitForSelector(dialog("confirm"));
      assert.match(await page.textContent(dialog("confirm")), /Your schedule changes haven.t been saved/);
      await page.click(`${dialog("confirm")} button:has-text("Cancel")`);
      await page.waitForSelector(dialog("confirm"), { state: "detached" });
      assert.equal(new URL(page.url()).hash, "#schedule/evening");
      assert.equal(await page.inputValue(`${row("sleep")} .trigger-time`), "22:30"); // the draft stays
      assert.equal(await page.inputValue(`${row("night")} .components .trigger-time >> nth=0`), "21:15");
      assert.equal(await stateText(page), "Unsaved changes");
    });

    await step("save: the config holds the edits", async () => {
      await page.click("#schedule-save");
      await page.waitForSelector("#notices .notice.ok");
      assert.match(
        await page.textContent("#notices .notice.ok"),
        /Saved the schedule profiles \(\d+ changes\)\. Nothing is deployed/,
      );
      await page.waitForFunction(() => document.querySelector("#schedule-state")?.textContent === "Saved");
      const evening = (await saved(page)).find((p) => p.id === "evening");
      assert.equal(evening.name, "Evenings");
      assert.deepEqual(evening.sections[5].trigger, { type: "clock", time: "22:30" });
      assert.equal(evening.sections[1].priority, 120);
    });

    await step("a new profile from the standard schedule, moved up, saved; a stale save is refused", async () => {
      await page.click("#new-profile");
      await page.waitForSelector(dialog("new-profile"));
      await page.fill(`${dialog("new-profile")} .new-profile-name`, "Weekend");
      await page.check(`${dialog("new-profile")} input[value="standard"]`);
      await page.click(`${dialog("new-profile")} button.confirm`);
      await page.waitForSelector('.profile-card[data-profile="weekend"]');
      assert.deepEqual(await listed(page), ["default", "evening", "weekend"]);
      assert.match(await page.textContent(".no-rooms"), /No adaptive room follows this profile yet/);
      assert.ok(await page.isEnabled(`${item("weekend")} .delete-profile`));
      await page.click(`${item("weekend")} .move-up`);
      assert.deepEqual(await listed(page), ["default", "weekend", "evening"]);
      await page.click("#schedule-save");
      await page.waitForFunction(() => document.querySelector("#schedule-state")?.textContent === "Saved");
      assert.deepEqual(
        (await saved(page)).map((p) => p.id),
        ["default", "weekend", "evening"],
      );

      // An edit here, while another tab saves first: this one is refused, not applied.
      await page.fill(".profile-name-input", "Weekend 2");
      await page.evaluate(async () => {
        const view = await (await fetch("/api/schedule_profiles")).json();
        const profiles = view.schedule_profiles;
        profiles[0].name = "Standard";
        await fetch("/api/schedule_profiles", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ schedule_profiles: profiles, base: view.version }),
        });
      });
      // Straight from the field to Save, as a hand would: the click must land.
      await page.click("#schedule-save");
      await page.waitForSelector("#notices .notice.error");
      assert.match(
        await page.textContent("#notices .notice.error"),
        /The schedule has changed since the schedule editor showed it/,
      );
      await page.waitForFunction(() => document.querySelector("#schedule-state")?.textContent === "Saved");
      assert.equal(await page.inputValue(".profile-name-input"), "Weekend"); // reloaded as it is
      assert.equal(await page.textContent(`${item("default")} .profile-label`), "Standard");
      await shot(page, "02-saved");
    });

    await step("at phone width the editor fits the screen", async () => {
      await page.setViewportSize({ width: 390, height: 860 });
      await page.goto(`${base}/#schedule/evening`);
      await page.waitForSelector(".profile-card .sections-table");
      await page.waitForTimeout(100);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 0, `the editor scrolls sideways by ${overflow}px`);
      assert.equal(await page.$eval(".sections-table thead", (t) => getComputedStyle(t).display), "none");
      await shot(page, "03-phone");
    });
  } catch (err) {
    errors.push(err.stack || String(err));
  }
  await browser.close();
  const ok = errors.length === 0;
  console.log(JSON.stringify({ ok, steps, errors }));
  process.exit(ok ? 0 : 1);
})();
