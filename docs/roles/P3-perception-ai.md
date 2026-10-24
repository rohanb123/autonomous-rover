# P3: Perception & AI (hybrid: ML, vision, mapper, map view, simulated assay)

## 1. Goal
Make the rover **choose like a scientist** and make the result **visible**:
- the models and priors that score rocks (`ml/`, vision `/score`);
- the placeholder "analysis" of a scooped sample plus the **simulated Bradford assay** (`/analyze`);
- the mapper that stores station bundles, publishes local tiles and fuses the **global map** (`mapper/`);
- the dashboard **MapView** and **BradfordPanel**.

> ⚠️ Still the heaviest role (voice moved to P4, arena photos to P1). The cut list in [05](../05-unknowns-and-risks.md#risk-register) says what to drop first. If you're behind at I2, P4 takes over `dashboard/src/assay/` as a whole directory.

## 2. Scope
| Yours | Not yours |
|---|---|
| `ml/**`, `gx10/vision/**`, `gx10/mapper/**` | Final score formula, heap, route, dedupe, exclusion (brain, P4). You return *components* |
| `dashboard/src/map/**`, `dashboard/src/assay/**` | Dashboard shell, panels, STDB connection (P4). You get rows via props |
| `mocks/fake_vision.py`, `mocks/fake_mapper.py` | Rock proposals on the Pi (P2). You score them |
| Ollama caption-model choice (:11434), recorded in `config/models.yaml`; the arena label layout in docs/03 (P1 captures and labels the photos) | DB schema/reducers (P4). You call `upsert_keyframe`, `upsert_local_tile`, `upsert_global_tile`, `set_run_fused`, `log_event` from the mapper |
| docs/03 | |

## 3. Hardware / environment
GX10 (ARM64, NVIDIA GB10, DGX OS, PyTorch preinstalled per the ASUS spec). Python 3.11+ venv with `open_clip_torch` ⚠️ (test before I0), `scikit-learn`, `pandas`, `fastapi`, `uvicorn`, `pillow`, `opencv-python-headless`, `numpy`, `httpx`. Ollama on :11434 with an optional caption VLM ⚠️ pull before the venue (template captions otherwise). No extra parts.

## 4. Files and function signatures
```python
# gx10/vision/embed.py
class Embedder:
    def __init__(self, model_name: str, pretrained: str, device: str = "cuda"): ...
    def embed(self, images: list[PIL.Image.Image]) -> np.ndarray      # (n, d), L2-normalized

# gx10/vision/heads.py
class Heads:
    @classmethod
    def load(cls, path: str = "ml/models/heads.joblib") -> "Heads"
    def visual(self, emb: np.ndarray) -> np.ndarray                   # (n,) P(science-interesting)
    def category_probs(self, emb: np.ndarray) -> list[dict[str, float]]   # keys: float_rock layered veined sand bedrock pebble
    def terrain(self, emb: np.ndarray) -> list[str]                    # soil|bedrock|sand|big_rock

# gx10/vision/novelty.py
class NoveltyMemory:              # one per run_id
    def score_and_add(self, emb: np.ndarray) -> np.ndarray             # 1 − max cos sim to memory (first item = 1.0)

# gx10/vision/pickable.py
def pickable(size_m: float, solidity: float | None = None) -> float     # 1.0 in [0.012, 0.030] m, linear falloff to 0 at 0.005/0.045

# gx10/vision/analyze.py
def analyze(image: PIL.Image.Image, crop_emb: np.ndarray | None) -> dict  # {label, confidence, caption}; caption via Ollama VLM or template

# gx10/vision/scoop_check.py
def scoop_has_item(present: np.ndarray, empty_ref: np.ndarray, roi: tuple[int,int,int,int], thr: float = 0.12) -> bool

# gx10/vision/bradford_sim.py   (§7)
def seed_for(sample_id: str, run_id: str = "") -> int
def simulate(sample_id: str, run_id: str = "", interest_score: float | None = None) -> dict

# gx10/vision/app.py  FastAPI :8100
POST /score   ScoreRequest -> ScoreResponse        (00 §6.6)
POST /analyze AnalyzeRequest -> AnalysisResult     (00 §6.7)  # always includes "assay" when scoop_has_item
GET  /health  -> {"ok": true, "model": str, "heads": str}

# gx10/mapper/store.py
class RunStore:
    def __init__(self, root: str = "/data/runs"): ...
    def save_station(self, run_id: str, station: dict, files: dict[str, bytes]) -> str
    def save_sample(self, run_id: str, sample: dict, jpg: bytes) -> str
    def url_for(self, rel_path: str) -> str                            # http://192.168.50.10:8200/files/runs/...

# gx10/mapper/tiles.py
def thumb_b64(jpg: bytes, max_px: int = 320, quality: int = 70) -> str
def png_b64(img: np.ndarray, max_px: int = 512) -> str
def slice_mosaic(mosaic: np.ndarray, origin_xy: tuple[float,float], res_m_px: float, tile_px: int = 512) -> list[dict]

# gx10/mapper/posegraph.py
def refine(stations: list[dict], overhead: list[dict], rel_yaw: list[tuple[str,str,float,float]]) -> dict[str, dict]
    # least squares over station poses: odometry edges + overhead absolute edges (when visible) + BEV-overlap yaw edges

# gx10/mapper/fuse.py
def fuse_run(run_id: str, res_m_px: float = 0.005) -> dict             # → mosaic.png + global tiles; returns {tiles, bounds, method}

# gx10/mapper/app.py  FastAPI :8200
POST /ingest/station   multipart (00 §6.4) → stores, upsert_keyframe ×N, upsert_local_tile, returns {ok, station_id, stored, urls}
POST /ingest/sample    multipart (00 §6.5) → {ok, url}
POST /fuse/{run_id}    → runs fuse_run, upsert_global_tile ×N, set_run_fused(run_id, true); returns {ok, tiles}
GET  /files/{path}     static files under /data

```
TypeScript (props in [06 §4](../06-dashboard.md#4-repo-layout)): `map/MapView.tsx`, `map/camera.ts` (`worldToScreen`, `screenToWorld`, `fitBounds`), `map/layers/*.ts` (`draw(ctx, cam, rows)`), `assay/BradfordPanel.tsx`, `assay/Cuvette.tsx`, `assay/StandardCurveChart.tsx`, `assay/format.ts` (`fmtConc`, `fmtA595`, `verdictLabel`, `a595ToColor`).

## 5. What the brain expects from you
- `/score` returns **components** (`visual`, `novelty`, `pickable`, `category_probs`, `terrain`, `embedding`). The brain computes `score` and `category_score` with your `category_priors.json` (00 §8). Novelty memory is per `run_id`, filled in the order items arrive.
- `/analyze` gets `interest_score` = the candidate's final score and returns label/caption + `assay`. If `scoop_has_item` is false → `assay` is omitted and the brain writes `assayType=""`.
- Mapper replies fast (< 1 s): do thumbnails/tiles synchronously but keep them small; fusion only on `/fuse`.

## 6. Mapping design (local → global)
- **Local map** = one BEV tile per station, stitched by P2 on the Pi, in the rover frame at the station pose. The mapper stores it and writes `local_map_tile` (pngB64 ≤ 512 px + URL). Live view draws these at the station pose.
- **Global map** (after `end_run`, `POST /fuse/{run_id}` from the brain):
  1. Gather station poses (dead-reckoned), overhead ArUco poses near each station's `t` (if any), and relative yaw between overlapping BEV tiles (`cv2.estimateAffinePartial2D` on ORB matches).
  2. `posegraph.refine` (small least squares, scipy or numpy) → corrected station poses. **Fallback:** no refinement (`method: "dr"`).
  3. Paint all BEV tiles into one mission-frame mosaic (newest-on-top or max-blend) at 0.005 m/px.
  4. Slice into 512 px tiles → `upsert_global_tile` (`version` += 1 on re-fuse) → `set_run_fused`.

## 7. Bradford assay simulator <a id="bradford-assay-simulator"></a>
> **SIMULATED — proof of concept.** No wet lab. No real protein is measured. The numbers are generated to look like a plausible Bradford readout and are labeled as such everywhere.

**Real parameters we copy (cited):**
| Parameter | Value | Source |
|---|---|---|
| Dye / readout | Coomassie Brilliant Blue G-250, read at **595 nm** | Thermo MAN0011203; Thermo TR0025; Bio-Rad 4110065A |
| Color forms | cationic red ~470 nm, neutral green ~650 nm, anionic blue **595 nm** when bound to protein; max shifts 465 → 595 nm | Bio-Rad 4110065A; Bio-Rad LIT33 |
| BSA standards | 2000, 1500, 1000, 750, 500, 250, 125, 25, 0 µg/mL (from a 2 mg/mL stock) | Thermo MAN0011203 |
| Linear working range (BSA, standard protocol) | **125–1000 µg/mL** | Thermo MAN0011203; Bio-Rad 4110065A |
| Protocol we "simulate" | 0.05 mL sample + 1.5 mL reagent, 10 min at room temperature, read 595 nm, subtract blank, read off the standard curve | Thermo MAN0011203 (test-tube protocol) |
| Curve shape | not linear across the full range; flattens at high concentration (dilute and re-assay) | Bio-Rad Bradford feature page; Bio-Rad 4110065A |

**Our own illustrative choices (not from a datasheet):** the hidden "true" response `A = 3.0·c / (3000 + c)` with Gaussian noise SD 0.01, the lognormal sample distribution, and the verdict cut at 500 µg/mL. They exist only to make realistic-looking numbers.

**Algorithm (`bradford_sim-1`):**
1. `seed = u32(first 4 bytes of sha256("{run_id}/{sample_id}"), big-endian)`; `rng = random.Random(seed)`.
2. Standards: triplicate readings at each BSA standard = true(c) + N(0, 0.01). Blank = mean of the c=0 triplicate; subtract it from all.
3. Fit a least-squares line over standards in 125–1000 µg/mL → slope, intercept, R².
4. Hidden sample concentration `c* = exp(N(ln(60 + 900·bias), 0.45))`, with `bias = clamp(interest_score, 0, 1)` (0.5 if missing). Higher-interest rocks tend to read higher; that is a *demo bias*, and the panel says so.
5. Sample A595 = max(true(c*) + N(0, 0.01) − blank, 0).
6. Concentration = max((A595 − intercept)/slope, 0), in µg/mL.
7. Verdict: `< 125` **below_detection**; `125–500` **low**; `≥ 500` **elevated**. `in_linear_range = 125 ≤ c ≤ 1000`.
8. Return the dict in 00 §6.7 (`assay_type="bradford_sim"`, `simulated=true`, `model_version="bradford_sim-1"`).

**Reference implementation** (verified to run and to be deterministic; copy into `gx10/vision/bradford_sim.py`):
```python
"""SIMULATED Bradford assay -- proof of concept. No wet lab. Deterministic per sample_id."""
from __future__ import annotations
import hashlib, json, math, random
from dataclasses import dataclass, asdict

WAVELENGTH_NM = 595                      # Thermo MAN0011203; Bio-Rad 4110065A
BSA_STANDARDS = [0, 25, 125, 250, 500, 750, 1000, 1500, 2000]  # µg/mL, Thermo Table 1
LINEAR_RANGE = (125.0, 1000.0)           # µg/mL, BSA, standard protocol (both vendors)
UNITS = "µg/mL"
# Illustrative "true" response used ONLY to synthesize numbers (ours, not a datasheet):
AMAX, K_HALF, NOISE_SD = 3.0, 3000.0, 0.01
REPLICATES = 3
MODEL_VERSION = "bradford_sim-1"

def seed_for(sample_id: str, run_id: str = "") -> int:
    return int.from_bytes(hashlib.sha256(f"{run_id}/{sample_id}".encode()).digest()[:4], "big")

def true_a595(c: float) -> float:
    return AMAX * c / (K_HALF + c)

def fit_linear(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    n = len(points); sx = sum(p[0] for p in points); sy = sum(p[1] for p in points)
    sxx = sum(p[0]**2 for p in points); sxy = sum(p[0]*p[1] for p in points)
    m = (n*sxy - sx*sy) / (n*sxx - sx*sx); b = (sy - m*sx) / n
    ybar = sy / n
    ss_tot = sum((y-ybar)**2 for _, y in points); ss_res = sum((y-(m*x+b))**2 for x, y in points)
    return m, b, 1 - ss_res/ss_tot

def verdict_for(c: float) -> str:
    if c < LINEAR_RANGE[0]: return "below_detection"
    if c < 500.0: return "low"
    return "elevated"

def simulate(sample_id: str, run_id: str = "", interest_score: float | None = None) -> dict:
    seed = seed_for(sample_id, run_id)
    rng = random.Random(seed)
    # 1) standards (blank-corrected, triplicates)
    raw = {c: [true_a595(c) + rng.gauss(0, NOISE_SD) for _ in range(REPLICATES)] for c in BSA_STANDARDS}
    blank = sum(raw[0]) / REPLICATES
    standards = [{"c": c, "a595": [round(a - blank, 4) for a in raw[c]]} for c in BSA_STANDARDS]
    pts = [(s["c"], a) for s in standards if LINEAR_RANGE[0] <= s["c"] <= LINEAR_RANGE[1] for a in s["a595"]]
    m, b, r2 = fit_linear(pts)
    # 2) hidden "true" protein in the sample extract, biased by interest score
    bias = 0.5 if interest_score is None else min(max(interest_score, 0.0), 1.0)
    true_c = math.exp(rng.gauss(math.log(60.0 + 900.0 * bias), 0.45))
    a = max(true_a595(true_c) + rng.gauss(0, NOISE_SD) - blank, 0.0)   # blank-corrected
    # 3) read back through the fitted standard curve
    conc = max((a - b) / m, 0.0)
    return {
        "assay_type": "bradford_sim", "simulated": True, "model_version": MODEL_VERSION,
        "seed": seed, "a595": round(a, 4), "concentration": round(conc, 1), "units": UNITS,
        "verdict": verdict_for(conc),
        "in_linear_range": LINEAR_RANGE[0] <= conc <= LINEAR_RANGE[1],
        "standard_curve_params": {
            "standard": "BSA", "wavelength_nm": WAVELENGTH_NM, "linear_range": list(LINEAR_RANGE),
            "true_model": {"type": "hyperbolic", "amax": AMAX, "k": K_HALF, "noise_sd": NOISE_SD},
            "fit": {"type": "linear", "slope": round(m, 7), "intercept": round(b, 4), "r2": round(r2, 4),
                    "fit_range": list(LINEAR_RANGE)},
            "blank_a595": round(blank, 4), "standards": standards},
    }

```

**Tests (`gx10/vision/tests/test_bradford_sim.py`):**
| Test | Expectation |
|---|---|
| Golden vector | `simulate("a_0001", "r_20261024_101500", 0.63)` → seed 3896889563, a595 0.2585, concentration 282.1, verdict `low`, fit slope 0.0007188, intercept 0.0557, r2 0.9947, blank −0.0068 |
| Determinism | two calls with the same args → identical dicts (`==`) |
| Different samples | `a_0001` vs `a_0002` same run → different seeds |
| Fit quality | r2 ≥ 0.98 for 200 random sample ids |
| Monotonic bias | over 300 ids, mean concentration at score 0.9 > at score 0.1 |
| Labels | every output has `simulated is True`, `assay_type == "bradford_sim"`, `units == "µg/mL"` |
| Distribution sanity | over 900 runs (300 ids × scores 0.1/0.5/0.9) all three verdicts appear (reference run: elevated 470, low 306, below_detection 124) |
| JSON safety | `seed < 2**32`; `json.dumps` round-trips |

**Dashboard panel:** see [06 §7](../06-dashboard.md#bradford-panel). It renders the stored row; it never re-simulates.

## 8. Build order, checkpoints, acceptance
| Done by | Build | ✅ Checkpoint |
|---|---|---|
| by I0 | Start AI4Mars subset + MSL v2.1 + ChemCam downloads; venv; `fake_vision.py`, `fake_mapper.py` (same contracts, canned answers, writes to STDB) | **I0:** P4's brain gets canned `/score` and `/analyze`; P2's uploader hits fake_mapper |
| by I1 | Test `open_clip` + Ollama (captions) on the GX10 (ARM64) | Each prints a result, or the fallback is chosen and noted in 05 |
| by I1 | `bradford_sim.py` + tests (golden vector); mapper `/ingest/*` + thumbnails + local tiles to STDB | **I1:** a fake_rover station appears in STDB `local_map_tile` + `keyframe`; golden test green |
| by I2 | chemcam_priors, embeddings, heads, `/score`, `/analyze` (template caption) | **I2:** real `/score` < 300 ms / 10 crops; metrics.json written |
| by I2 | MapView (tiles, trail, pins, exclusion, stations) against fake_brain | Live map renders the fake_brain run (06 D2, D10) |
| by I3 | Few-shot training + eval on P1's labeled arena photos | Arena accuracy reported |
| by I4 | `/fuse`: mosaic + global tiles (DR first, then posegraph) | **I3:** replay shows a fused global map for a real run |
| by I4 | BradfordPanel, Cuvette, StandardCurveChart | **I4:** 06 D6–D9 pass on a real sampled pin |
| by I5 | VLM captions (optional), polish, bug fixes | **I5 freeze** |

Acceptance summary: doc 03 §5 (models), the Bradford tests above, 06 D2/D5/D7–D10 (your components), and the mapper: a station upload of 8 keyframes + BEV returns in < 1 s and appears in STDB within 2 s.

## 9. Mocks
- **You provide** `fake_vision.py` (:8100; random-but-seeded components; `/analyze` calls the real `bradford_sim.simulate`, so the assay is real from I0) and `fake_mapper.py` (:8200; stores files, writes tiles from canned PNGs).
- **You use** `fake_rover.py` (P2) for station bundles and `fake_brain.py` (P4) to drive the dashboard map.

## 10. Watch-outs
- **Domain gap** (Mars vs arena): do the few-shot step; don't trust MSL-only heads on brown paper.
- Novelty memory must be **per run** and reset on `start_run`, or run 2 finds nothing novel.
- Keep base64 sizes under the reducer limits (thumb ≤ 150,000 chars, local tile ≤ 700,000).
- BEV rotation sign: the tile's image-up = rover +x; MapView rotates by `headingDeg` CCW. Test 06 D10 early.
- **Never** let the assay look real: "SIMULATED — proof of concept" in the panel, chart title and drawer chip; `simulated=true` is enforced by the reducer.
- Changing the simulator → bump `MODEL_VERSION` and update the golden vector in 00 §6.7 (contract change).

## 11. Contracts you need (copies; source of truth = [00](../00-overview-and-contracts.md))
**Ports:** vision `:8100`, mapper `:8200` (incl. `GET /health → {ok, stdb}`), Ollama `:11434`, STDB `:3000` (db `marsrover`), brain `:8000`. CORS for `http://192.168.50.30:5173` and `http://localhost:5173`.
**`/score`** request `{run_id, items:[{prop_id, crop_url, size_m, xy}]}` → `{items:[{prop_id, visual, novelty, pickable, category, category_probs{float_rock,layered,veined,sand,bedrock,pebble}, terrain, embedding[], model}]}`; embedding L2-normalized, any length.
**`/analyze`** request `{run_id, sample_id, cand_id, image_url, interest_score}` → `{sample_id, scoop_has_item, label, confidence, caption, simulated:true, assay{assay_type, simulated, model_version, seed, a595, concentration, units, verdict, in_linear_range, standard_curve_params{standard, wavelength_nm, linear_range, true_model, fit{type,slope,intercept,r2,fit_range}, blank_a595, standards[{c,a595[3]}]}}}`.
**Mission** `{mission_id, run_id, text, radius_m∈[0.3,3], k∈[1,8], categories⊆{float_rock,layered,veined,sand,bedrock,pebble,any}, sample, return_home, max_duration_s, mode∈{act,suggest}, assumptions[]}`.
**Station upload** `station.json {run_id, station_id, t, pose, keyframes[{kf_id,file,heading_deg (absolute mission frame; copy as is),t}], bev{file,res_m_px,w_px,h_px,frame:"rover@station"}, proposals[{prop_id,kf_id,bbox_px,crop,xy,size_m,contrast}]}` → `{ok, station_id, stored, urls{bev, crops{}}}`. Sample upload `{run_id, sample_id, cand_id, t, pose}` + `present.jpg` → `{ok, url}`.
**Reducers you call** (positional args): `upsert_keyframe(kfId, runId, stationId, tUnix, x, y, headingDeg, thumbB64, url)`, `upsert_local_tile(stationId, runId, tUnix, x, y, headingDeg, resMPx, wPx, hPx, pngB64, url)`, `upsert_global_tile(tileId, runId, ix, iy, originX, originY, resMPx, wPx, hPx, pngB64, version)`, `set_run_fused(runId, fused)`, `log_event(runId, tUnix, src, level, kind, msg, dataJson)`.
**analysis_record assay columns:** `assayType, a595, concentration, units, verdict, inLinearRange, standardCurveParams (JSON string), simulated (always true), seed u32, modelVersion`.
**Frames:** mission frame origin = home pad, +x = start heading, +y left, heading CCW+ degrees; BEV 0.005 m/px, image-up = rover +x.
**Scoring (brain, for reference):** `score = 0.35·novelty + 0.25·visual + 0.15·pickable + 0.15·category_score − 0.10·dist_norm`; `category_score = Σ probs[c]·prior[c]·want[c]`.
