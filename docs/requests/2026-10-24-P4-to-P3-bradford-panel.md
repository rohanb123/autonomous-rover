# Note: P4 filled the empty Bradford panel so the drawer can ship

**From:** Rohan (P4) · **To:** Aditi (P3) · **Date:** 2026-10-24
**Branch:** `p4/replay-panels`
**Status:** done on this branch

`dashboard/src/assay/` is yours. The files were empty on `main`, and the pin drawer needed the panel for the demo, so this branch fills them to docs/06 §7:

- `BradfordPanel.tsx` default export, props `{ record: AnalysisRecord; autoplay: boolean }`
- `Cuvette.tsx`, `StandardCurveChart.tsx`, `format.ts`, `assay.css`, `assay.test.ts`

The panel only draws the `analysis_record` row. It does not re-simulate. Take the files back whenever you want to change the look.
