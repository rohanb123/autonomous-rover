# 06: Dashboard (one app, live + replay)

**App owner: P4** (shell, routing, STDB connection, brain API client, all panels except the two below).
**P3 owns** `dashboard/src/map/` (the map canvas and its layers) and `dashboard/src/assay/` (simulated Bradford assay panel).
Every component below has exactly one owner.

## 1. Stack
| Choice | Why |
|---|---|
| **React + Vite + TypeScript (strict)** | `spacetimedb/react` gives `SpacetimeDBProvider` + `useTable`; Vite dev server on :5173 |
| `spacetimedb` npm package + generated `src/module_bindings/` | The only supported way to subscribe ([02 §9](02-spacetimedb.md#9-subscriptions-dashboard-dashboardsrcstdb)) |
| **Canvas 2D** for the map (no map library) | Our world is a 3 × 3 m arena in metres; image tiles + polylines + circles. A slippy-map library adds nothing |
| Plain SVG for the standard-curve chart (no chart lib) | ~9 points + a line; fewer deps, full control over labels |
| CSS: one `styles.css` with CSS variables; no UI framework | Speed |
| `fetch` to brain REST; `MediaRecorder` for push-to-talk (`audio/webm`) | Browser built-ins |

Env: `VITE_STDB_URI=ws://192.168.50.10:3000`, `VITE_STDB_DB=marsrover`, `VITE_BRAIN_URL=http://192.168.50.10:8000`, `VITE_MAPPER_URL=http://192.168.50.10:8200`.

## 2. Modes
- **Live**: subscriptions for the active run (`run.status = active`). The map shows local tiles as they arrive, the rover glyph and trail, candidates, top-k ranks, visited circles. Controls are enabled.
- **Replay**: pick any run. Global tiles (if `run.fused`), else the local tiles. A **timeline scrubber** sets `scrubT`; every layer and panel shows rows with `tUnix ≤ scrubT`. Controls are disabled (read-only). Clicking a pin opens the **PinDetailDrawer** (photo, score breakdown, reason, analysis, and the **BradfordPanel** for sampled pins). Reaching a sample's `tUnix` while playing auto-opens it.
- **Earth/Mars** (live only): in Mars mode the brain delays commands; the dashboard *also* delays what it shows by `effectiveDelayS` (renders rows with `tUnix ≤ now − delay`), with a banner "Signal from Mars: 10.0 s old".

## 3. Layout (wireframe)
```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ TopBar: ARES SCOUT │ RunPicker ▼ │ [LIVE|REPLAY] │ ConnectionBadge ●STDB ●Brain ●Rover │ E-STOP ■ │
├──────────────────────────────────────────────┬───────────────────────────────────────────────┤
│                                              │ MissionPanel                                  │
│                                              │  "Survey 1.5 m, top 3 rocks…"  radius 1.5     │
│              MapView (P3)                    │  k 3 · cats float_rock · act · assumptions    │
│   tiles · trail · rover glyph · stations     │  [🎙 hold to talk] [text box ⏎]               │
│   candidate pins (rank #) · exclusion rings  ├───────────────────────────────────────────────┤
│   scale bar · north/+x arrow · zoom/pan      │ TaskQueuePanel (top-k + queue)                │
│                                              │  #1 c_0007 0.63 float_rock "Novel texture…"   │
│                                              │  #2 c_0012 0.58 …     status: queued          │
│                                              ├───────────────────────────────────────────────┤
│                                              │ KeyframePanel (latest kf thumb + heading)     │
├──────────────────────────────────────────────┼───────────────────────────────────────────────┤
│ QuickBar: [pause][resume][stop][skip][status][come back]  │ DelayPanel: Earth◯ Mars●     │
│                                              │  compression ──●── 74.7×  OWLT 746.6 s →10.0 s │
├──────────────────────────────────────────────┴───────────────────────────────────────────────┤
│ TimelineScrubber (replay): |──●──────── events ticks ─────| ▶ 1× 4×   t = 10:21:34 PDT       │
│ HealthPanel: state STATION · cpu 55 °C · link ok · uplink backlog 0 │ EventLog (last 50)     │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

PinDetailDrawer (slides in from the right, replay or live):
┌────────────────────────────────────────────┐
│ c_0007 · float_rock · visited (sampled)    │
│ [crop photo]  [present.jpg]                │
│ score 0.63 = nov .64·.35 + vis .71·.25 …  │
│ reason: "Novel texture (0.64), pickable…" │
│ analysis: label float_rock (0.62)          │
│ caption: "A small angular dark rock…"      │
│ ┌ BradfordPanel (P3) ───────────────────┐  │
│ │ ⚠ SIMULATED — proof of concept        │  │
│ │ [cuvette brown→blue]  A595 = 0.2585    │  │
│ │ standard curve (BSA) ● sample          │  │
│ │ 282 µg/mL · verdict: LOW               │  │
│ └────────────────────────────────────────┘  │
└────────────────────────────────────────────┘
```

## 4. Repo layout
```
dashboard/src/
  main.tsx                P4  SpacetimeDBProvider (memoized builder), router
  App.tsx                 P4  layout grid, mode state, scrubT state, selected pin
  stdb/connection.ts      P4  builder, connection status events
  stdb/hooks.ts           P4  useRunTables(runId) → all filtered useTable calls; useScrubbed(rows, scrubT)
  api/brain.ts            P4  typed REST client (§6.11 of doc 00)
  panels/*.tsx            P4  (list in §5)
  map/                    P3  MapView.tsx, camera.ts (world↔screen), layers/{tiles,trail,pins,exclusion,stations}.ts
  assay/                  P3  BradfordPanel.tsx, Cuvette.tsx, StandardCurveChart.tsx, format.ts, assay.test.ts
  module_bindings/        generated by scripts/gen_bindings.sh (P4); never edited
```
**Interface between P4 and P3 components** (props only; P3 components never open their own connection):
```ts
// map/MapView.tsx (P3)
export interface MapViewProps {
  mode: 'live' | 'replay';
  poses: Pose[]; localTiles: LocalMapTile[]; globalTiles: GlobalTile[];
  candidates: Candidate[]; visits: Visit[]; status?: RoverStatus;
  selectedCandId?: string; onSelectCand(candId: string): void;
}
// assay/BradfordPanel.tsx (P3)
export interface BradfordPanelProps { record: AnalysisRecord; autoplay: boolean; }
```
Row types come from `module_bindings`.

## 5. Components, owners and data bindings
| Component | Owner | STDB tables (subscribed via `useRunTables`) | Brain REST used | Notes |
|---|---|---|---|---|
| TopBar / RunPicker | P4 | `run` | `POST /api/run/start`, `/api/run/end` | RunPicker lists runs newest first |
| ConnectionBadge | P4 | `rover_status.updatedUnix` | `GET /api/health` (every 2 s) | STDB: connect/disconnect callbacks. Rover: stale if `now − updatedUnix > 2 s`. Brain: health fetch fails → red |
| EStopButton | P4 | `rover_status.estop` | `POST /api/estop`, `/api/estop/clear` | Always enabled in live mode; **never delayed**; keyboard: Space bar (with confirm on clear only) |
| MissionPanel + VoiceButton | P4 | `mission` | `/api/mission/voice`, `/api/mission/text`, `/api/approve` | Shows parsed params and `assumptions`; "arrives on Mars in 8.2 s" countdown from `deliverAtUnix` |
| TaskQueuePanel | P4 | `task_queue`, `candidate` (rank ≥ 0) | — | Top-k sorted by rank with score, category, reason; queue below |
| KeyframePanel | P4 | `keyframe` (latest by tUnix) | — | `data:image/jpeg;base64,` + thumbB64; click → open `url` |
| QuickBar | P4 | `rover_status.fsmState` | `/api/quick` | Disabled in replay |
| DelayPanel | P4 | `delay_config` | `/api/delay` | Log slider 1–1000; shows OWLT (s, from Horizons) and effective delay |
| HealthPanel | P4 | `telemetry` (latest), `rover_status` | — | |
| EventLog | P4 | `event` | — | Last 50, level-colored |
| TimelineScrubber | P4 | `pose`, `event`, `analysis_record` (ticks) | — | Range = run start…end; play at 1×/4×/16× |
| PinDetailDrawer | P4 | `candidate`, `visit`, `analysis_record`, `keyframe` | — | Hosts BradfordPanel when an `analysis_record` with `assayType = "bradford_sim"` exists |
| **MapView + layers** | **P3** | via props: `pose`, `local_map_tile`, `global_tile`, `candidate`, `visit`, `rover_status` | — | tiles drawn rotated by `headingDeg` at (x, y), scaled by `resMPx` |
| **BradfordPanel / Cuvette / StandardCurveChart** | **P3** | via props: one `analysis_record` row | — | §7 |

No component writes to STDB. No component talks to the rover.

## 6. Map rendering rules (P3)
- World = mission frame metres. `camera.ts`: `worldToScreen(x, y)`, with +x **up** on screen (matches "forward" at start) and +y left. Default view fits all poses + 0.5 m.
- **Local tile** (BEV, rover frame at capture): draw image centered at (x, y), rotated by `headingDeg`, size `wPx·resMPx` m. Newer tiles drawn on top. Opacity 0.85.
- **Global tile** (replay, mission frame, axis-aligned): draw at `originX, originY` with `resMPx`. Hidden in live mode unless `run.fused`.
- **Trail**: polyline of poses (color by source: dr gray, aruco green, fused white). Rover glyph = triangle at the latest pose (or at the scrubbed pose).
- **Pins**: candidates colored by status (new gray, queued yellow, active orange, visited green, skipped/excluded dim). Rank number inside top-k pins. Size ∝ score.
- **Exclusion rings**: circle of `radiusM` at each visit, dashed.
- **Stations**: small square at each `local_map_tile` (x, y).
- Hit-testing for pin click: within 12 px.

## 7. Simulated Bradford assay panel (P3) <a id="bradford-panel"></a>
**Data:** everything comes from the `analysis_record` row (`a595`, `concentration`, `units`, `verdict`, `inLinearRange`, `seed`, `standardCurveParams` JSON). **The browser never re-simulates**, so replays reproduce by construction. The Python simulator ([P3 brief](roles/P3-perception-ai.md#bradford-assay-simulator)) is the only source of numbers.

**Label:** a fixed red-outlined banner **"SIMULATED — proof of concept"** at the top of the panel, in the chart title, and as alt text on the cuvette. Also in the PinDetailDrawer header chip ("analysis: SIMULATED"). It can't be hidden.

**Cuvette animation (`Cuvette.tsx`)**: an SVG cuvette. On open (or on reaching the sample time in replay with `autoplay`), the liquid animates over **1.5 s** from brown (`#8B5A2B`, unbound acidic dye, Amax 465–470 nm) to a blue whose intensity maps to `a595`: `t = clamp(a595 / 1.2, 0, 1)`, color = lerp(`#8B5A2B` → `#1F3FBF`, ease-out(t))`. Show the caption "Coomassie G-250: 465/470 nm (red-brown) → 595 nm (blue) on protein binding". `prefers-reduced-motion` = jump to the final color.

**Standard curve (`StandardCurveChart.tsx`)**: SVG, x = BSA µg/mL (0–2000), y = A595 (blank-corrected).
- Points: each standard's replicates (small dots) + mean (larger dot).
- Line: the fitted linear curve over `fit.fit_range` (125–1000), dashed extension outside.
- Shaded band for the linear range 125–1000 µg/mL.
- The sample: horizontal guide at `a595` → intersection → vertical guide down to `concentration`, sample dot in accent color.
- Footer: `y = {slope}·x + {intercept}, R² = {r2}` and the citation line "Parameters: 595 nm, BSA standards, linear range per Thermo Fisher MAN0011203 / Bio-Rad 4110065A".

**Readout**: `282 µg/mL` (0 decimals ≥ 100, 1 decimal < 100), `A595 = 0.2585`, `seed 3896889563`, and a **verdict band**: a horizontal bar with three zones, **below detection** (< 125), **low** (125–500), **elevated** (≥ 500), with a marker at the concentration. If `!inLinearRange`: note "outside linear range: a real lab would dilute and re-assay".

## 8. Acceptance tests
| # | Test | Owner |
|---|---|---|
| D1 | `npm run build` passes with `strict: true`, zero TS errors | P4 |
| D2 | With `mocks/fake_brain.py` writing a scripted run, Live shows pose trail, 3 local tiles, ≥ 5 candidates with ranks, 2 visits with rings, the latest keyframe, within 1 s of each write | P4 + P3 (map) |
| D3 | Killing STDB turns the STDB dot red within 3 s; stopping fake_brain status writes turns Rover red within 3 s | P4 |
| D4 | E-STOP click → `POST /api/estop` fires immediately in Mars mode with compression 1 (12+ min delay) | P4 |
| D5 | Replay: the scrubber at t₀ shows only rows with `tUnix ≤ t₀`; dragging back hides later pins | P4 + P3 |
| D6 | Click a sampled pin → drawer with photo, score breakdown, reason, analysis, BradfordPanel | P4 (drawer), P3 (panel) |
| D7 | BradfordPanel renders the golden record (00 §6.7) → A595 0.2585 (4 decimals), 282 µg/mL, verdict LOW, sample dot on the line between the 250 and 500 standards | P3 |
| D8 | Opening the same pin twice (or reloading) shows identical numbers and final color (deterministic) | P3 |
| D9 | "SIMULATED — proof of concept" visible in panel, chart title and drawer chip (screenshot check) | P3 |
| D10 | Map: a tile with `headingDeg = 90` is drawn rotated so the rover's forward points to screen-left (+y) | P3 |

## 9. Pitfalls
- **Memoize the connection builder** (`useMemo(..., [])`) or it reconnects every render.
- `useTable` returns `[rows, isReady]`; render a skeleton until `isReady`.
- BigInt ids (`id: u64`): `.toString()` before keys/JSON.
- Re-subscribe on run change; old run rows otherwise linger in the cache.
- Base64 images: create `data:` URLs once per row (memoize by id) and decode into `ImageBitmap` for canvas; don't re-decode every frame.
- Canvas: handle `devicePixelRatio` or tiles look blurry.
- Mixed content: the dashboard on http talks to `ws://`; don't serve it over https.
- Never compute anything that changes behavior in the browser; the brain owns logic.
