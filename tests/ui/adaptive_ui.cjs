// Drives the Now, Analysis and Almanac tabs (P5.2b) in headless Chromium against a running
// Light Map over a fake HA (started by tests/test_ui.py) with the cutover switch off, as the
// live app runs until P6. The living room's store holds three weeks of readings and the
// snapshot the nightly pass made of them; HA holds the old system's al_ entities, which the
// tabs must never show. Checks the schedule and the not-live state, the analysis chart (drawn,
// legible, keyboard-readable), the almanac, the room kept across tabs, and the phone layout.
// Pragmatic DOM checks, not a browser suite. Prints one JSON line ({ok, steps, errors}) and
// exits non-zero on failure.
//
//   node tests/ui/adaptive_ui.cjs http://127.0.0.1:PORT [screenshot-dir]

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

const texts = (page, selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

(async () => {
  const browser = await chromium.launch(process.env.LM_CHROMIUM ? { executablePath: process.env.LM_CHROMIUM } : {});
  // A locale of its own: the vendored chart library reads the browser's, and a bare
  // container's ("en-US@posix") isn't one Chromium's Intl accepts.
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: "en-GB" });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  try {
    await step("Now: today's schedule, the section in force, and adaptive not live yet", async () => {
      await page.goto(`${base}/#now`);
      await page.waitForSelector(".schedule-table tbody tr");
      // The one adaptive room is picked, and the address says so.
      assert.equal(new URL(page.url()).hash, "#now/living_room");
      assert.equal(await page.getAttribute('a.room-pill[data-room="living_room"]', "aria-current"), "page");
      assert.equal(await page.getAttribute('a[href="#now"].tab', "aria-current"), "page");
      assert.match(await page.textContent(".view-head h2"), /Now · Living Room/);
      // The not-live state, in place of the live fields: nothing of the old system's.
      const live = await page.textContent(".live-card");
      assert.match(live, /Adaptive runs at cutover — not live yet/);
      assert.equal(await page.$(".live-card .live-state"), null);
      assert.doesNotMatch(live, /al_|raised|Night/);
      // The schedule, in HA's time zone, with the section in force highlighted (not by
      // colour alone: a NOW badge and aria-current).
      assert.match(await page.textContent(".view-head .when"), /12:00 \(Europe\/London\)/);
      const rows = await page.$$eval(".schedule-table tbody tr", (trs) =>
        trs.map((tr) => [tr.dataset.section, tr.querySelector("td.start").textContent.trim()]),
      );
      assert.deepEqual(rows, [
        ["sunrise", "05:30"],
        ["day", "10:25"],
        ["afternoon", "15:06"],
        ["sunset", "18:06"],
        ["night", "20:30"],
        ["sleep", "22:00"],
      ]);
      assert.equal(await page.getAttribute('.schedule-table tr[data-section="day"]', "aria-current"), "time");
      assert.match(await page.textContent('.schedule-table tr[data-section="day"] .badge.now'), /NOW/);
      assert.match(await page.textContent('.schedule-table tr[data-section="sunrise"]'), /the earlier of sunrise and 05:30/);
      assert.match(await page.textContent('.schedule-table tr[data-section="sleep"]'), /into tomorrow/);
      // The day bar: the section in force outlined, the time marked.
      assert.equal(await page.getAttribute(".day-bar .segment.current", "data-section"), "day");
      assert.ok(await page.$(".day-bar .now-marker"));
      assert.match(await page.getAttribute(".day-bar", "aria-label"), /Day from 10:25.*It is 12:00/);
      // The profile, and the way to edit it.
      assert.match(await page.textContent(".profile-line"), /Follows the profile evening/);
      assert.equal(await page.getAttribute("a.edit-schedule", "href"), "#schedule/evening");
      // The targets now, from the store's almanac (a learned brightness for each unit).
      const targets = await texts(page, ".targets-table tbody .target-cell");
      assert.equal(targets.length, 2);
      for (const t of targets) assert.match(t, /^\d+ · \d+%$/);
      assert.match(await page.textContent(".lux-line"), /\d+(\.\d)? lx \(learned\)/);
      await shot(page, "01-now");
    });

    await step("Analysis: the room is kept, and the learning state is shown", async () => {
      await page.click('a[href="#analysis"].tab');
      await page.waitForSelector(".analysis-table tbody tr");
      assert.equal(new URL(page.url()).hash, "#analysis/living_room");
      assert.match(await page.textContent(".state-card .pill.mode"), /Learning/);
      assert.match(await page.textContent(".state-card"), /days analysed/);
      assert.match(await page.textContent(".adaptive-body .intro"), /records nothing/);
      const sections = await page.$$eval(".analysis-table tbody tr", (trs) => trs.map((tr) => tr.dataset.section));
      assert.deepEqual(sections, ["sunrise", "day", "afternoon", "sunset", "night", "sleep"]);
      assert.equal(await page.$$eval(".analysis-table tbody tr.learned", (trs) => trs.length), 6);
    });

    await step("Analysis: the charts are drawn, legible, and readable without colour", async () => {
      // uPlot draws into a canvas: one for the units' brightness, one for the lux target.
      await page.waitForSelector(".brightness-card .chart .uplot canvas");
      await page.waitForSelector(".lux-card .chart .uplot canvas");
      const box = await page.$eval(".brightness-card .chart canvas", (c) => [c.clientWidth, c.clientHeight]);
      assert.ok(box[0] > 600 && box[1] >= 200, `the chart is ${box}`);
      // Something was drawn: the canvas isn't blank.
      const inked = await page.$eval(".brightness-card .chart canvas", (c) => {
        const data = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
        let n = 0;
        for (let i = 3; i < data.length; i += 4) if (data[i]) n += 1;
        return n;
      });
      assert.ok(inked > 1000, `only ${inked} pixels drawn`);
      // The legend names each unit beside its line, dash and marker as drawn: not colour alone.
      assert.deepEqual(await texts(page, ".brightness-card .chart-legend li"), ["Chandelier", "Reading"]);
      const dashes = await page.$$eval(".brightness-card .chart-legend line", (lines) =>
        lines.map((l) => l.getAttribute("stroke-dasharray")),
      );
      assert.equal(dashes[0], null);
      assert.ok(dashes[1], "the second unit's line is dashed");
      const strokes = await page.$$eval(".brightness-card .chart-legend line", (lines) => lines.map((l) => l.getAttribute("stroke")));
      assert.notEqual(strokes[0], strokes[1]);
      assert.ok(strokes.every((s) => /^#[0-9a-f]{6}$/i.test(s)), `strokes from the theme: ${strokes}`);
      // Labelled for a screen reader, which the table under it serves.
      assert.match(await page.getAttribute(".brightness-card .chart", "aria-label"), /Learned brightness by section for Chandelier, Reading/);
      // The keyboard reads it: focus, then the arrows walk the sections.
      await page.focus(".brightness-card .chart");
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("ArrowRight");
      await page.waitForFunction(() => /^Day:/.test(document.querySelector(".brightness-card .chart-readout").textContent));
      assert.match(await page.textContent(".brightness-card .chart-readout"), /^Day: \d+ \(\d+%\) Chandelier · \d+ \(\d+%\) Reading$/);
      // And so does the pointer.
      const plot = await page.$(".lux-card .chart .u-over");
      const rect = await plot.boundingBox();
      await page.mouse.move(rect.x + 2, rect.y + rect.height / 2);
      await page.waitForFunction(() => /^Sunrise:/.test(document.querySelector(".lux-card .chart-readout").textContent));
      assert.match(await page.textContent(".lux-card .chart-readout"), /^Sunrise: \d+(\.\d)? lx$/);
      await shot(page, "02-analysis");
    });

    await step("Almanac: the store's snapshot in force, never HA's sensor", async () => {
      await page.click('a[href="#almanac"].tab');
      await page.waitForSelector(".almanac-table tbody tr");
      assert.equal(new URL(page.url()).hash, "#almanac/living_room");
      assert.match(await page.textContent(".almanac-source"), /never from Home Assistant.s sensor\.al_almanac_/);
      assert.match(await page.textContent(".snapshot"), /In force since\s*2026-10-14/);
      assert.match(await page.textContent(".snapshot"), /Learning/);
      const day = await page.$$eval('.almanac-table tr[data-section="day"] td', (tds) => tds.map((td) => td.textContent.trim()));
      assert.match(day[0], /^\d+ · \d+%since 2026-10-14$/);
      assert.deepEqual(day.slice(2), [day[2], "on", "5 lx", "4%", "120 s"]);
      assert.match(day[2], /lx \(learned\)/);
      const night = await page.$eval('.almanac-table tr[data-section="night"] td[data-knob="Maintain"]', (td) => td.textContent);
      assert.equal(night, "off");
      await shot(page, "03-almanac");
    });

    await step("a room that isn't adaptive is not offered; an unknown one falls back", async () => {
      await page.goto(`${base}/#now/kitchen`);
      await page.waitForSelector(".schedule-table");
      assert.equal(new URL(page.url()).hash, "#now/living_room");
      assert.deepEqual(await texts(page, "a.room-pill"), ["Living Room"]);
      assert.match(await page.textContent("#notices"), /no adaptive room “kitchen”/);
    });

    await step("at phone width every tab fits the screen", async () => {
      await page.setViewportSize({ width: 390, height: 860 });
      for (const tab of ["now", "analysis", "almanac"]) {
        await page.goto(`${base}/#${tab}/living_room`);
        await page.waitForSelector(tab === "now" ? ".schedule-table" : tab === "analysis" ? ".brightness-card canvas" : ".almanac-table");
        await page.waitForTimeout(100);
        assert.ok((await overflow(page)) <= 0, `${tab} scrolls sideways at phone width`);
        if (tab === "analysis") {
          // The chart follows the width it is given.
          const width = await page.$eval(".brightness-card .chart canvas", (c) => c.clientWidth);
          assert.ok(width <= 370, `the chart is ${width}px wide`);
        }
      }
      // The schedule table stacks, each cell labelled.
      await page.goto(`${base}/#now/living_room`);
      await page.waitForSelector(".schedule-table");
      assert.equal(await page.$eval(".schedule-table thead", (t) => getComputedStyle(t).display), "none");
      assert.ok((await overflow(page)) <= 0);
      // A segment label that wouldn't fit is hidden, never clipped.
      const clipped = await page.$$eval(".day-bar .segment .label:not([hidden])", (labels) =>
        labels.filter((l) => l.offsetWidth > l.parentElement.clientWidth).length,
      );
      assert.equal(clipped, 0);
      await shot(page, "04-phone");
    });
  } catch (err) {
    errors.push(err.stack || String(err));
  }
  await browser.close();
  const ok = errors.length === 0;
  console.log(JSON.stringify({ ok, steps, errors }));
  process.exit(ok ? 0 : 1);
})();
