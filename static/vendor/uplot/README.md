# uPlot 1.6.31 (vendored)

The Analysis tab's charts (P5.2b). Vendored as published, unmodified: no CDN, no build
step, nothing fetched at runtime. The same version and build the predecessor
(ha-adaptive-light) vendored.

- `uPlot.iife.min.js` — `dist/uPlot.iife.min.js` from the npm package `uplot@1.6.31`
  (sha256 `2d27e8ad3d228164525ce213f9dc716f39b4e3aee0cc773fb3491c96cf4921a2`); defines the
  global `uPlot`.
- `uPlot.min.css` — `dist/uPlot.min.css` from the same package
  (sha256 `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04`).
- `LICENSE` — its MIT licence. Source: https://github.com/leeoniya/uPlot

To upgrade, replace both files from the new release's `dist/`, update the hashes and the
version here, and run the UI tests (`tests/ui/adaptive_ui.cjs` draws the chart).
