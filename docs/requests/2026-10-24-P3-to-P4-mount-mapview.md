# Request: P3 → P4: mount MapView in App.tsx

**From:** Aditi (P3) · **To:** Rohan (P4) · **Date:** 2026-10-24
**Code:** branch `p3/mapview`, `dashboard/src/map/MapView.tsx`
**Status:** done (P4, 2026-10-24). Mounted in `dashboard/src/App.tsx` (b377eda); the conditional-import workaround was replaced with a direct `import MapView from './map/MapView'` once the default export landed.

MapView is built to the docs/06 §4 `MapViewProps` interface (arrays typed `readonly`, so the rows
from `useRunTables` pass straight in). `App.tsx` is yours, so I haven't touched it. To mount it,
replace the `.map-placeholder` block inside `<main className="map-area">` with:

```tsx
import MapView from './map/MapView';
// ...
const [selectedCandId, setSelectedCandId] = useState<string>();
// ...
<main className="map-area">
  <MapView
    mode={mode}
    poses={t.poses}
    localTiles={t.localTiles}
    globalTiles={t.globalTiles}
    candidates={t.candidates}
    visits={t.visits}
    status={roverStatus?.runId === runId ? roverStatus : undefined}
    selectedCandId={selectedCandId}
    onSelectCand={setSelectedCandId}
  />
</main>
```

Notes for you to decide:
1. **Replay scrubbing.** MapView draws whatever rows it gets. In replay, pass rows filtered with
   your `useScrubbed(rows, scrubT)`, as docs/06 §2 says. `global_tile` has no `tUnix`, so decide
   whether to hide global tiles while `scrubT` is before the run's end.
2. **Fused flag.** `MapViewProps` has no `fused` field, so MapView treats "global tiles exist" as
   fused (they're only written by `/fuse`, right before `set_run_fused`). If you'd rather pass
   `run.fused` explicitly, add an optional `fused?: boolean` to the props in docs/06 §4 and tell me.
   I'll read it.
3. **Selection.** `onSelectCand` fires on a pin click (12 px hit radius). It's the natural trigger
   for PinDetailDrawer.

Preview without STDB: `cd dashboard && npm run dev`, then open
`http://localhost:5173/src/map/preview.html` (add `?mode=replay&fused=1` for the fused view).
